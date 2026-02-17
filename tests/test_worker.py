from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wowkmang.config import GlobalConfig, ProjectConfig
from wowkmang.docker_runner import ContainerResult
from wowkmang.hooks import HookResult
from wowkmang.models import Task, TaskSource, TaskSourceInfo, TaskStatus, task_to_yaml
from wowkmang.queue import ensure_queue_dirs, save_task
from wowkmang.summary import PRMetadata
from wowkmang.worker import Worker


def _make_project(**overrides) -> ProjectConfig:
    defaults = {
        "name": "testproj",
        "repo": "https://github.com/user/project",
        "credentials": {"github_token": "ghp_test"},
        "pre_task": [],
        "post_task": ["pytest"],
        "post_task_policy": "warn",
    }
    defaults.update(overrides)
    return ProjectConfig(**defaults)


def _make_task(**overrides) -> Task:
    defaults = {
        "project": "testproj",
        "repo": "https://github.com/user/project",
        "task": "Fix the bug",
        "source": TaskSourceInfo(type=TaskSource.GITHUB_ISSUE, issue_number=42),
    }
    defaults.update(overrides)
    return Task(**defaults)


def _make_config(tmp_path: Path) -> GlobalConfig:
    return GlobalConfig(
        tasks_dir=tmp_path / "tasks",
        projects_dir=tmp_path / "projects",
        cache_volume="test-cache",
        host_claude_config_dir="/home/user/.claude",
        keep_workdir=False,
    )


@pytest.fixture
def setup(tmp_path):
    config = _make_config(tmp_path)
    ensure_queue_dirs(config.tasks_dir)
    project = _make_project()
    projects = {"testproj": project}

    docker_runner = MagicMock()
    docker_runner.run_claude_code.return_value = ContainerResult(
        exit_code=0, logs="done"
    )
    docker_runner.create_volume.side_effect = [
        "wowkmang-work-abc123",
        "wowkmang-session-def456",
    ]
    docker_runner.copy_to_workdir.return_value = ContainerResult(
        exit_code=0, logs="copied"
    )

    def _run_git_side_effect(**kwargs):
        command = kwargs.get("command", "")
        if "git diff origin/" in command and "--quiet" in command:
            # Simulate "has changes" so pipeline proceeds to push/PR
            return ContainerResult(exit_code=1, logs="")
        return ContainerResult(exit_code=0, logs="ok")

    docker_runner.run_git.side_effect = _run_git_side_effect

    repo_cache = MagicMock()
    repo_cache.prepare_workdir.return_value = "wowkmang/abc12345"

    hook_runner = MagicMock()
    hook_runner.run_hooks.return_value = HookResult(
        success=True, output="ok", exit_code=0
    )

    fix_loop = MagicMock()

    summary_gen = MagicMock()
    summary_gen.generate.return_value = PRMetadata(
        title="Fix login bug",
        branch="wowkmang/fix-login-42",
        description="Closes #42",
    )

    worker = Worker(
        config=config,
        projects=projects,
        docker_runner=docker_runner,
        repo_cache=repo_cache,
        hook_runner=hook_runner,
        fix_loop=fix_loop,
        summary_generator=summary_gen,
    )

    return {
        "config": config,
        "projects": projects,
        "project": project,
        "docker_runner": docker_runner,
        "repo_cache": repo_cache,
        "hook_runner": hook_runner,
        "fix_loop": fix_loop,
        "summary_gen": summary_gen,
        "worker": worker,
        "tmp_path": tmp_path,
    }


def _save_and_pick(config, task):
    """Save a task and move it to running (simulating pick_next_task)."""
    from wowkmang.queue import pick_next_task

    save_task(config.tasks_dir, task)
    result = pick_next_task(config.tasks_dir)
    assert result is not None
    return result


def _patch_github(func):
    """Decorator to patch GitHubClient in worker tests."""

    @patch("wowkmang.worker.GitHubClient")
    def wrapper(self, MockGH, setup):
        return func(self, MockGH, setup)

    return wrapper


class TestProcessTask:
    @_patch_github
    def test_successful_full_pipeline(self, MockGH, setup):
        mock_gh = MagicMock()
        mock_gh.create_pr.return_value = {
            "number": 87,
            "html_url": "https://github.com/user/project/pull/87",
        }
        MockGH.return_value = mock_gh

        task = _make_task()
        task_file, task = _save_and_pick(setup["config"], task)

        setup["worker"]._process_task(task_file, task)

        # Task ended up in done/
        done_files = list((setup["config"].tasks_dir / "done").glob("*.yaml"))
        assert len(done_files) == 1

        # PR was created
        mock_gh.create_pr.assert_called_once()
        call_kwargs = mock_gh.create_pr.call_args.kwargs
        assert call_kwargs["draft"] is False
        assert call_kwargs["branch"] == "wowkmang/fix-login-42"

        # Push was done via docker_runner.run_git
        push_calls = [
            c
            for c in setup["docker_runner"].run_git.call_args_list
            if "git push" in str(c)
        ]
        assert len(push_calls) >= 1

        mock_gh.add_labels.assert_called()

    @_patch_github
    def test_pre_hook_failure_fails_task(self, MockGH, setup):
        setup["hook_runner"].run_hooks.return_value = HookResult(
            success=False, output="install failed", exit_code=1
        )
        setup["project"].pre_task = ["uv sync"]

        task = _make_task()
        task_file, task = _save_and_pick(setup["config"], task)

        setup["worker"]._process_task(task_file, task)

        # Task in failed/
        failed_files = list((setup["config"].tasks_dir / "failed").glob("*.yaml"))
        assert len(failed_files) == 1

        # Claude Code was never called
        setup["docker_runner"].run_claude_code.assert_not_called()

    @_patch_github
    def test_claude_code_failure_fails_task(self, MockGH, setup):
        setup["docker_runner"].run_claude_code.return_value = ContainerResult(
            exit_code=1, logs="error"
        )

        task = _make_task()
        task_file, task = _save_and_pick(setup["config"], task)

        setup["worker"]._process_task(task_file, task)

        failed_files = list((setup["config"].tasks_dir / "failed").glob("*.yaml"))
        assert len(failed_files) == 1

    @_patch_github
    def test_post_hook_fail_with_fail_policy(self, MockGH, setup):
        setup["hook_runner"].run_hooks.return_value = HookResult(
            success=False, output="tests failed", exit_code=1
        )
        setup["project"].post_task_policy = "fail"

        task = _make_task()
        task_file, task = _save_and_pick(setup["config"], task)

        setup["worker"]._process_task(task_file, task)

        failed_files = list((setup["config"].tasks_dir / "failed").glob("*.yaml"))
        assert len(failed_files) == 1

        # No PR created
        MockGH.return_value.create_pr.assert_not_called()

    @_patch_github
    def test_post_hook_fail_with_warn_policy_creates_draft_pr(self, MockGH, setup):
        setup["hook_runner"].run_hooks.return_value = HookResult(
            success=False, output="tests failed", exit_code=1
        )
        setup["project"].post_task_policy = "warn"

        mock_gh = MagicMock()
        mock_gh.create_pr.return_value = {"number": 88, "html_url": "url"}
        MockGH.return_value = mock_gh

        task = _make_task()
        task_file, task = _save_and_pick(setup["config"], task)

        setup["worker"]._process_task(task_file, task)

        # Task completed (in done/, not failed/)
        done_files = list((setup["config"].tasks_dir / "done").glob("*.yaml"))
        assert len(done_files) == 1

        # PR was draft
        call_kwargs = mock_gh.create_pr.call_args.kwargs
        assert call_kwargs["draft"] is True

    @_patch_github
    def test_fix_or_fail_enters_fix_loop_then_fails(self, MockGH, setup):
        setup["hook_runner"].run_hooks.return_value = HookResult(
            success=False, output="fail", exit_code=1
        )
        setup["fix_loop"].run.return_value = HookResult(
            success=False, output="still failing", exit_code=1
        )
        setup["project"].post_task_policy = "fix_or_fail"

        task = _make_task()
        task_file, task = _save_and_pick(setup["config"], task)

        setup["worker"]._process_task(task_file, task)

        setup["fix_loop"].run.assert_called_once()

        failed_files = list((setup["config"].tasks_dir / "failed").glob("*.yaml"))
        assert len(failed_files) == 1

    @_patch_github
    def test_fix_or_warn_enters_fix_loop_then_drafts(self, MockGH, setup):
        setup["hook_runner"].run_hooks.return_value = HookResult(
            success=False, output="fail", exit_code=1
        )
        setup["fix_loop"].run.return_value = HookResult(
            success=False, output="still failing", exit_code=1
        )
        setup["project"].post_task_policy = "fix_or_warn"

        mock_gh = MagicMock()
        mock_gh.create_pr.return_value = {"number": 89, "html_url": "url"}
        MockGH.return_value = mock_gh

        task = _make_task()
        task_file, task = _save_and_pick(setup["config"], task)

        setup["worker"]._process_task(task_file, task)

        setup["fix_loop"].run.assert_called_once()

        done_files = list((setup["config"].tasks_dir / "done").glob("*.yaml"))
        assert len(done_files) == 1

        call_kwargs = mock_gh.create_pr.call_args.kwargs
        assert call_kwargs["draft"] is True

    @_patch_github
    def test_fix_loop_success_creates_regular_pr(self, MockGH, setup):
        setup["hook_runner"].run_hooks.return_value = HookResult(
            success=False, output="fail", exit_code=1
        )
        setup["fix_loop"].run.return_value = HookResult(
            success=True, output="pass", exit_code=0
        )
        setup["project"].post_task_policy = "fix_or_warn"

        mock_gh = MagicMock()
        mock_gh.create_pr.return_value = {"number": 90, "html_url": "url"}
        MockGH.return_value = mock_gh

        task = _make_task()
        task_file, task = _save_and_pick(setup["config"], task)

        setup["worker"]._process_task(task_file, task)

        call_kwargs = mock_gh.create_pr.call_args.kwargs
        assert call_kwargs["draft"] is False

    def test_unknown_project_fails_task(self, setup):
        task = _make_task(project="nonexistent")
        task_file, task = _save_and_pick(setup["config"], task)

        setup["worker"]._process_task(task_file, task)

        failed_files = list((setup["config"].tasks_dir / "failed").glob("*.yaml"))
        assert len(failed_files) == 1

    @_patch_github
    def test_pulls_image_once_per_task(self, MockGH, setup):
        mock_gh = MagicMock()
        mock_gh.create_pr.return_value = {"number": 91, "html_url": "url"}
        MockGH.return_value = mock_gh

        task = _make_task()
        task_file, task = _save_and_pick(setup["config"], task)

        setup["worker"]._process_task(task_file, task)

        setup["docker_runner"].ensure_image.assert_called_once_with(
            setup["project"].docker_image, setup["project"]
        )


class TestErrorMessages:
    @patch("wowkmang.worker.GitHubClient")
    def test_pipeline_exception_recorded_in_task(self, MockGH, setup):
        mock_gh = MagicMock()
        mock_gh.create_pr.side_effect = RuntimeError("branch has no new commits")
        MockGH.return_value = mock_gh

        task = _make_task()
        task_file, task = _save_and_pick(setup["config"], task)

        setup["worker"]._process_task(task_file, task)

        failed_files = list((setup["config"].tasks_dir / "failed").glob("*.yaml"))
        assert len(failed_files) == 1
        content = failed_files[0].read_text()
        assert "branch has no new commits" in content


class TestKeepWorkdir:
    @patch("wowkmang.worker.GitHubClient")
    def test_workdir_deleted_by_default(self, MockGH, setup):
        mock_gh = MagicMock()
        mock_gh.create_pr.return_value = {"number": 1, "html_url": "url"}
        MockGH.return_value = mock_gh

        task = _make_task()
        task_file, task = _save_and_pick(setup["config"], task)

        setup["worker"]._process_task(task_file, task)

        # remove_volume called for work volume (which contains both code and session)
        remove_calls = setup["docker_runner"].remove_volume.call_args_list
        volume_names = [str(c.args[0]) for c in remove_calls]
        assert any("work" in v for v in volume_names)

    @patch("wowkmang.worker.GitHubClient")
    def test_workdir_preserved_when_keep_workdir(self, MockGH, setup):
        setup["config"].keep_workdir = True
        setup["worker"].config = setup["config"]

        mock_gh = MagicMock()
        mock_gh.create_pr.return_value = {"number": 1, "html_url": "url"}
        MockGH.return_value = mock_gh

        task = _make_task()
        task_file, task = _save_and_pick(setup["config"], task)

        setup["worker"]._process_task(task_file, task)

        # remove_volume should NOT be called for work volume (it contains both code and session)
        remove_calls = setup["docker_runner"].remove_volume.call_args_list
        volume_names = [str(c.args[0]) for c in remove_calls]
        assert not any("work" in v for v in volume_names)


class TestCrashRecovery:
    def test_recovers_stale_tasks_to_pending(self, setup):
        task = _make_task()
        task.attempts = 0
        task.max_attempts = 3

        # Place task directly in running/
        running_dir = setup["config"].tasks_dir / "running"
        task_file = running_dir / f"2025-01-01T00-00-00_{task.id}.yaml"
        task_file.write_text(task_to_yaml(task))

        setup["worker"]._recover_stale_tasks()

        # Should be back in pending/
        pending_files = list((setup["config"].tasks_dir / "pending").glob("*.yaml"))
        assert len(pending_files) == 1
        assert not task_file.exists()

    def test_fails_task_at_max_attempts(self, setup):
        task = _make_task()
        task.attempts = 2
        task.max_attempts = 3

        running_dir = setup["config"].tasks_dir / "running"
        task_file = running_dir / f"2025-01-01T00-00-00_{task.id}.yaml"
        task_file.write_text(task_to_yaml(task))

        setup["worker"]._recover_stale_tasks()

        # Should be in failed/
        failed_files = list((setup["config"].tasks_dir / "failed").glob("*.yaml"))
        assert len(failed_files) == 1
        pending_files = list((setup["config"].tasks_dir / "pending").glob("*.yaml"))
        assert len(pending_files) == 0

    def test_no_crash_if_running_dir_empty(self, setup):
        # Should not raise
        setup["worker"]._recover_stale_tasks()

    def test_kills_stale_containers_on_recovery(self, setup):
        setup["worker"]._recover_stale_tasks()
        setup["docker_runner"].kill_stale_containers.assert_called_once()


class TestSeedClaudeConfig:
    def test_calls_seed_volume(self, tmp_path):
        config = _make_config(tmp_path)
        config.host_claude_config_dir = "/home/user/.claude"
        ensure_queue_dirs(config.tasks_dir)

        docker_runner = MagicMock()
        docker_runner.seed_volume.return_value = ContainerResult(exit_code=0, logs="")

        worker = Worker(
            config=config,
            projects={},
            docker_runner=docker_runner,
            repo_cache=MagicMock(),
            hook_runner=MagicMock(),
            fix_loop=MagicMock(),
            summary_generator=MagicMock(),
        )

        worker._seed_claude_config("work-vol-123", "img:latest")

        docker_runner.seed_volume.assert_called_once_with(
            image="img:latest",
            source_host_path="/home/user/.claude",
            target_volume="work-vol-123",
            target_path="/workspace/.claude-config",
        )

    def test_missing_config_dir_does_not_call_seed(self, tmp_path):
        config = _make_config(tmp_path)
        config.host_claude_config_dir = ""
        ensure_queue_dirs(config.tasks_dir)

        docker_runner = MagicMock()

        worker = Worker(
            config=config,
            projects={},
            docker_runner=docker_runner,
            repo_cache=MagicMock(),
            hook_runner=MagicMock(),
            fix_loop=MagicMock(),
            summary_generator=MagicMock(),
        )

        worker._seed_claude_config("session-vol", "img")

        docker_runner.seed_volume.assert_not_called()


class TestCommitChanges:
    def test_calls_run_git_with_commit_script(self, tmp_path):
        config = _make_config(tmp_path)
        ensure_queue_dirs(config.tasks_dir)

        docker_runner = MagicMock()
        docker_runner.run_git.return_value = ContainerResult(exit_code=0, logs="")

        worker = Worker(
            config=config,
            projects={},
            docker_runner=docker_runner,
            repo_cache=MagicMock(),
            hook_runner=MagicMock(),
            fix_loop=MagicMock(),
            summary_generator=MagicMock(),
        )

        worker._commit_changes("work-vol", "img:latest")

        docker_runner.run_git.assert_called_once()
        call_kwargs = docker_runner.run_git.call_args.kwargs
        assert call_kwargs["work_volume"] == "work-vol"
        assert call_kwargs["image"] == "img:latest"
        assert "git" in call_kwargs["command"]
        assert "add -A" in call_kwargs["command"]
        assert "commit" in call_kwargs["command"]
        assert "user.name=wowkmang" in call_kwargs["command"]

    def test_nonzero_exit_raises(self, tmp_path):
        config = _make_config(tmp_path)
        ensure_queue_dirs(config.tasks_dir)

        docker_runner = MagicMock()
        docker_runner.run_git.return_value = ContainerResult(exit_code=1, logs="error")

        worker = Worker(
            config=config,
            projects={},
            docker_runner=docker_runner,
            repo_cache=MagicMock(),
            hook_runner=MagicMock(),
            fix_loop=MagicMock(),
            summary_generator=MagicMock(),
        )

        with pytest.raises(RuntimeError, match="Commit failed"):
            worker._commit_changes("work-vol", "img")


class TestExtractRepo:
    def test_standard_url(self):
        assert Worker._extract_repo("https://github.com/user/project") == "user/project"

    def test_url_with_git_suffix(self):
        assert (
            Worker._extract_repo("https://github.com/user/project.git")
            == "user/project"
        )


class TestNoChanges:
    @patch("wowkmang.worker.GitHubClient")
    def test_no_diff_skips_push_and_pr(self, MockGH, setup):
        """When _has_changes returns False, skip push/PR and complete with note."""

        def _run_git_no_changes(**kwargs):
            command = kwargs.get("command", "")
            if "git diff origin/" in command and "--quiet" in command:
                # exit_code=0 means no diff
                return ContainerResult(exit_code=0, logs="")
            return ContainerResult(exit_code=0, logs="ok")

        setup["docker_runner"].run_git.side_effect = _run_git_no_changes

        task = _make_task()
        task_file, task = _save_and_pick(setup["config"], task)

        setup["worker"]._process_task(task_file, task)

        # Task completed (in done/)
        done_files = list((setup["config"].tasks_dir / "done").glob("*.yaml"))
        assert len(done_files) == 1

        # No push was attempted (no call with "git push")
        push_calls = [
            c
            for c in setup["docker_runner"].run_git.call_args_list
            if "git push" in str(c)
        ]
        assert len(push_calls) == 0

        # No PR was created
        MockGH.return_value.create_pr.assert_not_called()

        # Task result has "No changes produced"
        content = done_files[0].read_text()
        assert "No changes produced" in content

    @_patch_github
    def test_with_changes_proceeds_to_push(self, MockGH, setup):
        """When _has_changes returns True, push and create PR normally."""
        mock_gh = MagicMock()
        mock_gh.create_pr.return_value = {
            "number": 99,
            "html_url": "https://github.com/user/project/pull/99",
        }
        MockGH.return_value = mock_gh

        task = _make_task()
        task_file, task = _save_and_pick(setup["config"], task)

        setup["worker"]._process_task(task_file, task)

        # PR was created
        mock_gh.create_pr.assert_called_once()

        # Push happened
        push_calls = [
            c
            for c in setup["docker_runner"].run_git.call_args_list
            if "git push" in str(c)
        ]
        assert len(push_calls) >= 1


class TestLogStep:
    def test_log_step_writes_to_steps_log(self, tmp_path):
        """_log_step calls run_git to append to steps.log."""
        config = _make_config(tmp_path)
        ensure_queue_dirs(config.tasks_dir)

        docker_runner = MagicMock()
        docker_runner.run_git.return_value = ContainerResult(exit_code=0, logs="")

        worker = Worker(
            config=config,
            projects={},
            docker_runner=docker_runner,
            repo_cache=MagicMock(),
            hook_runner=MagicMock(),
            fix_loop=MagicMock(),
            summary_generator=MagicMock(),
        )

        step_result = ContainerResult(exit_code=0, logs="step output here")
        worker._log_step("test_step", step_result, "work-vol", "img:latest")

        docker_runner.run_git.assert_called_once()
        call_kwargs = docker_runner.run_git.call_args.kwargs
        assert call_kwargs["work_volume"] == "work-vol"
        assert call_kwargs["image"] == "img:latest"
        assert "steps.log" in call_kwargs["command"]

    def test_log_step_includes_step_name_and_exit_code(self, tmp_path):
        """The log entry contains the step name and exit code."""
        config = _make_config(tmp_path)
        ensure_queue_dirs(config.tasks_dir)

        docker_runner = MagicMock()
        docker_runner.run_git.return_value = ContainerResult(exit_code=0, logs="")

        worker = Worker(
            config=config,
            projects={},
            docker_runner=docker_runner,
            repo_cache=MagicMock(),
            hook_runner=MagicMock(),
            fix_loop=MagicMock(),
            summary_generator=MagicMock(),
        )

        step_result = ContainerResult(exit_code=42, logs="some output")
        worker._log_step("my_step", step_result, "vol", "img")

        command = docker_runner.run_git.call_args.kwargs["command"]
        assert "my_step" in command
        assert "exit_code=42" in command


class TestHasChanges:
    def test_returns_true_when_diff_exists(self, tmp_path):
        config = _make_config(tmp_path)
        ensure_queue_dirs(config.tasks_dir)

        docker_runner = MagicMock()
        docker_runner.run_git.return_value = ContainerResult(exit_code=1, logs="")

        worker = Worker(
            config=config,
            projects={},
            docker_runner=docker_runner,
            repo_cache=MagicMock(),
            hook_runner=MagicMock(),
            fix_loop=MagicMock(),
            summary_generator=MagicMock(),
        )

        assert worker._has_changes("work-vol", "main", "img") is True

    def test_returns_false_when_no_diff(self, tmp_path):
        config = _make_config(tmp_path)
        ensure_queue_dirs(config.tasks_dir)

        docker_runner = MagicMock()
        docker_runner.run_git.return_value = ContainerResult(exit_code=0, logs="")

        worker = Worker(
            config=config,
            projects={},
            docker_runner=docker_runner,
            repo_cache=MagicMock(),
            hook_runner=MagicMock(),
            fix_loop=MagicMock(),
            summary_generator=MagicMock(),
        )

        assert worker._has_changes("work-vol", "main", "img") is False


class TestCopyToWorkdir:
    @_patch_github
    def test_copy_to_workdir_called_in_pipeline(self, MockGH, setup):
        """Verify copy_to_workdir is called during the pipeline."""
        mock_gh = MagicMock()
        mock_gh.create_pr.return_value = {"number": 100, "html_url": "url"}
        MockGH.return_value = mock_gh

        task = _make_task()
        task_file, task = _save_and_pick(setup["config"], task)

        setup["worker"]._process_task(task_file, task)

        setup["docker_runner"].copy_to_workdir.assert_called_once()
        call_kwargs = setup["docker_runner"].copy_to_workdir.call_args.kwargs
        assert "work_volume" in call_kwargs
        assert (
            "session_volume" not in call_kwargs
        )  # session is now consolidated into work_volume
        assert call_kwargs["cache_subdir"] == "github.com_user_project"

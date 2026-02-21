from unittest.mock import MagicMock, call

import pytest

from wowkmang.api.config import ProjectConfig
from wowkmang.executor.docker_runner import ContainerResult
from wowkmang.executor.hooks import HookResult, HookRunner, HookType
from wowkmang.taskqueue.models import Task, TaskSource, TaskSourceInfo


def _make_project(**overrides) -> ProjectConfig:
    defaults = {
        "name": "test",
        "repo": "https://github.com/u/p",
        "post_task": ["uv run pytest"],
        "max_fix_attempts": 2,
    }
    defaults.update(overrides)
    return ProjectConfig(**defaults)


def _make_task(**overrides) -> Task:
    defaults = {
        "project": "test",
        "repo": "https://github.com/u/p",
        "task": "Fix bug",
        "source": TaskSourceInfo(type=TaskSource.API),
    }
    defaults.update(overrides)
    return Task(**defaults)


class TestHookRunner:
    def test_hooks_success(self):
        docker_runner = MagicMock()
        docker_runner.run_hooks.return_value = ContainerResult(exit_code=0, logs="ok")
        hook_runner = HookRunner(docker_runner)
        project = _make_project()

        result = hook_runner.run_hooks(HookType.POST, "/work", "proj-vol", project)

        assert result.success is True
        assert result.exit_code == 0
        assert result.output == "ok"

    def test_hooks_failure(self):
        docker_runner = MagicMock()
        docker_runner.run_hooks.return_value = ContainerResult(
            exit_code=1, logs="install failed"
        )
        hook_runner = HookRunner(docker_runner)
        project = _make_project()

        result = hook_runner.run_hooks(HookType.POST, "/work", "proj-vol", project)

        assert result.success is False
        assert result.exit_code == 1
        assert result.output == "install failed"

    def test_run_hooks_pre_uses_pre_task(self):
        docker_runner = MagicMock()
        docker_runner.run_hooks.return_value = ContainerResult(exit_code=0, logs="")
        hook_runner = HookRunner(docker_runner)
        project = _make_project(pre_task=["uv sync"])

        hook_runner.run_hooks(HookType.PRE, "/work", "proj-vol", project)

        _, _, commands, _ = docker_runner.run_hooks.call_args.args
        assert commands == ["uv sync"]

    def test_run_hooks_post_uses_post_task(self):
        docker_runner = MagicMock()
        docker_runner.run_hooks.return_value = ContainerResult(exit_code=0, logs="")
        hook_runner = HookRunner(docker_runner)
        project = _make_project(post_task=["uv run pytest"])

        hook_runner.run_hooks(HookType.POST, "/work", "proj-vol", project)

        _, _, commands, _ = docker_runner.run_hooks.call_args.args
        assert commands == ["uv run pytest"]


class TestHookRunnerPreCommit:
    def test_has_pre_commit_returns_true_when_config_exists(self):
        docker_runner = MagicMock()
        docker_runner.run_command.return_value = ContainerResult(exit_code=0, logs="")
        hook_runner = HookRunner(docker_runner)
        project = _make_project()

        assert hook_runner.has_pre_commit("/work", "proj-vol", project) is True

    def test_has_pre_commit_returns_false_when_no_config(self):
        docker_runner = MagicMock()
        docker_runner.run_command.return_value = ContainerResult(exit_code=1, logs="")
        hook_runner = HookRunner(docker_runner)
        project = _make_project()

        assert hook_runner.has_pre_commit("/work", "proj-vol", project) is False

    def test_run_pre_commit_calls_pre_commit_run_a(self):
        docker_runner = MagicMock()
        docker_runner.run_hooks.return_value = ContainerResult(exit_code=0, logs="ok")
        hook_runner = HookRunner(docker_runner)
        project = _make_project()

        result = hook_runner.run_pre_commit("/work", "proj-vol", project)

        assert result.success is True
        _, _, commands, _ = docker_runner.run_hooks.call_args.args
        assert commands == ["pre-commit run -a"]

    def test_run_pre_commit_returns_failure_on_nonzero_exit(self):
        docker_runner = MagicMock()
        docker_runner.run_hooks.return_value = ContainerResult(
            exit_code=1, logs="hook failed"
        )
        hook_runner = HookRunner(docker_runner)
        project = _make_project()

        result = hook_runner.run_pre_commit("/work", "proj-vol", project)

        assert result.success is False
        assert result.output == "hook failed"

    def test_stage_changes_calls_git_add_a(self):
        docker_runner = MagicMock()
        docker_runner.run_command.return_value = ContainerResult(exit_code=0, logs="")
        hook_runner = HookRunner(docker_runner)
        project = _make_project()

        hook_runner.stage_changes("/work", "proj-vol", project)

        docker_runner.run_command.assert_called_once()
        call_kwargs = docker_runner.run_command.call_args.kwargs
        assert call_kwargs["command"] == ["git", "add", "-A"]
        assert call_kwargs["work_dir"] == "/work"
        assert call_kwargs["project_volume"] == "proj-vol"


class TestRunPostTaskChecks:
    def _make_runner_with_pre_commit(self, pre_commit_results, post_hook_result=None):
        """Helper: docker_runner where run_command returns exit_code=0 (has pre-commit config)
        and run_hooks returns the given results in sequence."""
        docker_runner = MagicMock()
        docker_runner.run_command.return_value = ContainerResult(exit_code=0, logs="")
        if pre_commit_results is not None:
            docker_runner.run_hooks.side_effect = [
                ContainerResult(exit_code=r, logs=f"run_{i}")
                for i, r in enumerate(pre_commit_results)
            ]
            if post_hook_result is not None:
                docker_runner.run_hooks.side_effect = [
                    ContainerResult(exit_code=r, logs=f"pc_{i}")
                    for i, r in enumerate(pre_commit_results)
                ] + [ContainerResult(exit_code=post_hook_result, logs="post")]
        return docker_runner

    def test_no_pre_commit_no_post_hooks_succeeds(self):
        docker_runner = MagicMock()
        docker_runner.run_command.return_value = ContainerResult(exit_code=1, logs="")
        docker_runner.run_hooks.return_value = ContainerResult(exit_code=0, logs="")
        hook_runner = HookRunner(docker_runner)
        project = _make_project(post_task=[])

        result = hook_runner.run_post_task_checks("/work", "proj-vol", project)

        assert result.success is True

    def test_pre_commit_runs_twice_with_stage_between(self):
        docker_runner = MagicMock()
        # run_command: first call checks pre-commit config (exit 0 = exists),
        # subsequent calls are git add -A
        docker_runner.run_command.return_value = ContainerResult(exit_code=0, logs="")
        docker_runner.run_hooks.return_value = ContainerResult(exit_code=0, logs="ok")
        hook_runner = HookRunner(docker_runner)
        project = _make_project(post_task=[])

        call_order = []
        docker_runner.run_hooks.side_effect = lambda *a, **kw: (
            call_order.append("pre_commit") or ContainerResult(exit_code=0, logs="ok")
        )
        docker_runner.run_command.side_effect = lambda **kw: (
            call_order.append(
                "git_add" if kw.get("command") == ["git", "add", "-A"] else "check"
            )
            or ContainerResult(exit_code=0, logs="")
        )

        hook_runner.run_post_task_checks("/work", "proj-vol", project)

        # pre-commit runs twice, git add happens between them
        assert call_order.count("pre_commit") == 2
        first_pc = call_order.index("pre_commit")
        last_pc = len(call_order) - 1 - call_order[::-1].index("pre_commit")
        git_add_idx = call_order.index("git_add")
        assert first_pc < git_add_idx < last_pc

    def test_pre_commit_verify_failure_returns_failure(self):
        docker_runner = MagicMock()
        docker_runner.run_command.return_value = ContainerResult(exit_code=0, logs="")
        # 1st run: auto-fix (exits 1), 2nd run: verify (exits 1), post hooks not reached
        docker_runner.run_hooks.side_effect = [
            ContainerResult(exit_code=1, logs="fixed files"),
            ContainerResult(exit_code=1, logs="still failing"),
        ]
        hook_runner = HookRunner(docker_runner)
        project = _make_project(post_task=[])

        result = hook_runner.run_post_task_checks("/work", "proj-vol", project)

        assert result.success is False
        assert result.output == "still failing"

    def test_pre_commit_verify_success_runs_post_hooks(self):
        docker_runner = MagicMock()
        docker_runner.run_command.return_value = ContainerResult(exit_code=0, logs="")
        # 1st pre-commit: auto-fix, 2nd: verify passes, then post hooks
        docker_runner.run_hooks.side_effect = [
            ContainerResult(exit_code=1, logs="fixed"),
            ContainerResult(exit_code=0, logs="clean"),
            ContainerResult(exit_code=0, logs="tests passed"),
        ]
        hook_runner = HookRunner(docker_runner)
        project = _make_project(post_task=["uv run pytest"])

        result = hook_runner.run_post_task_checks("/work", "proj-vol", project)

        assert result.success is True
        assert docker_runner.run_hooks.call_count == 3

    def test_post_hook_failure_returns_failure(self):
        docker_runner = MagicMock()
        docker_runner.run_command.return_value = ContainerResult(exit_code=1, logs="")
        docker_runner.run_hooks.return_value = ContainerResult(
            exit_code=1, logs="tests failed"
        )
        hook_runner = HookRunner(docker_runner)
        project = _make_project(post_task=["uv run pytest"])

        result = hook_runner.run_post_task_checks("/work", "proj-vol", project)

        assert result.success is False
        assert result.output == "tests failed"

from unittest.mock import MagicMock, patch

import pytest

from wowkmang.config import ProjectConfig
from wowkmang.docker_runner import ContainerResult, DockerRunner


def _make_project(**overrides) -> ProjectConfig:
    defaults = {
        "name": "test",
        "repo": "https://github.com/u/p",
        "credentials": {"GITHUB_TOKEN": "ghp_secret"},
    }
    defaults.update(overrides)
    return ProjectConfig(**defaults)


def _mock_container(exit_code: int = 0, logs: bytes = b"output"):
    container = MagicMock()
    container.wait.return_value = {"StatusCode": exit_code}
    container.logs.return_value = logs
    return container


def _mock_docker_client(container=None):
    client = MagicMock()
    if container is None:
        container = _mock_container()
    client.containers.run.return_value = container
    return client


class TestRunClaudeCode:
    def test_calls_containers_run_with_correct_args(self):
        container = _mock_container()
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client, cache_volume="test-cache")
        project = _make_project()

        result = runner.run_claude_code(
            work_dir="work-vol-123",
            task_prompt="Fix the bug",
            model="sonnet",
            project=project,
            timeout_minutes=30,
        )

        docker_client.containers.run.assert_called_once()
        kwargs = docker_client.containers.run.call_args.kwargs
        assert kwargs["image"] == project.docker_image
        assert kwargs["working_dir"] == "/workspace/repo"
        assert kwargs["detach"] is True
        assert kwargs["mem_limit"] == "4g"
        assert kwargs["environment"]["CLAUDE_MODEL"] == "sonnet"
        assert kwargs["environment"]["GITHUB_TOKEN"] == "ghp_secret"
        assert "work-vol-123" in kwargs["volumes"]

    def test_returns_container_result(self):
        container = _mock_container(exit_code=0, logs=b"all done")
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client, cache_volume="test-cache")

        result = runner.run_claude_code(
            work_dir="work-vol",
            task_prompt="task",
            model="sonnet",
            project=_make_project(),
            timeout_minutes=30,
        )

        assert isinstance(result, ContainerResult)
        assert result.exit_code == 0
        assert result.logs == "all done"

    def test_nonzero_exit_code_returned(self):
        container = _mock_container(exit_code=1, logs=b"error occurred")
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client, cache_volume="test-cache")

        result = runner.run_claude_code(
            work_dir="/w",
            task_prompt="task",
            model="sonnet",
            project=_make_project(),
            timeout_minutes=10,
        )

        assert result.exit_code == 1
        assert result.logs == "error occurred"

    def test_command_is_list(self):
        container = _mock_container()
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client, cache_volume="test-cache")

        runner.run_claude_code(
            work_dir="/w",
            task_prompt="Fix the bug",
            model="sonnet",
            project=_make_project(),
            timeout_minutes=10,
        )

        command = docker_client.containers.run.call_args.kwargs["command"]
        assert isinstance(command, list)
        assert command[0] == "sh"
        assert command[1] == "-c"
        assert "mkdir -p /root/.claude" in command[2]
        assert command[3] == "--"
        assert command[4] == "claude"
        assert "--model" in command
        assert "sonnet" in command
        assert "Fix the bug" in command

    def test_extra_instructions_prepended_to_prompt(self):
        container = _mock_container()
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client, cache_volume="test-cache")
        project = _make_project(extra_instructions="Always write tests.")

        runner.run_claude_code(
            work_dir="/w",
            task_prompt="Fix the bug",
            model="sonnet",
            project=project,
            timeout_minutes=10,
        )

        command = docker_client.containers.run.call_args.kwargs["command"]
        # The prompt (last element) should contain both instructions and task
        prompt = command[-1]
        assert "Always write tests." in prompt
        assert "Fix the bug" in prompt

    def test_timeout_kills_container(self):
        container = _mock_container()
        container.wait.side_effect = Exception("timeout")
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client, cache_volume="test-cache")

        with pytest.raises(Exception, match="timeout"):
            runner.run_claude_code(
                work_dir="/w",
                task_prompt="task",
                model="sonnet",
                project=_make_project(),
                timeout_minutes=1,
            )

        container.kill.assert_called_once()

    def test_container_removed_on_success(self):
        container = _mock_container()
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client, cache_volume="test-cache")

        runner.run_claude_code(
            work_dir="/w",
            task_prompt="t",
            model="s",
            project=_make_project(),
            timeout_minutes=1,
        )

        container.remove.assert_called_once()

    def test_container_removed_on_exception(self):
        container = _mock_container()
        container.wait.side_effect = RuntimeError("boom")
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client, cache_volume="test-cache")

        with pytest.raises(RuntimeError):
            runner.run_claude_code(
                work_dir="/w",
                task_prompt="t",
                model="s",
                project=_make_project(),
                timeout_minutes=1,
            )

        container.remove.assert_called_once()

    def test_cache_volume_mounted(self):
        container = _mock_container()
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client, cache_volume="my-cache-vol")

        runner.run_claude_code(
            work_dir="work-vol",
            task_prompt="t",
            model="s",
            project=_make_project(),
            timeout_minutes=1,
        )

        volumes = docker_client.containers.run.call_args.kwargs["volumes"]
        assert "my-cache-vol" in volumes
        assert volumes["my-cache-vol"]["bind"] == "/cache"

    def test_container_labeled(self):
        container = _mock_container()
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client, cache_volume="test-cache")

        runner.run_claude_code(
            work_dir="/w",
            task_prompt="t",
            model="s",
            project=_make_project(),
            timeout_minutes=1,
        )

        labels = docker_client.containers.run.call_args.kwargs["labels"]
        assert labels == {"wowkmang": "true"}

    def test_continue_session_flag(self):
        container = _mock_container()
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client, cache_volume="test-cache")

        runner.run_claude_code(
            work_dir="/w",
            task_prompt="summarize",
            model="haiku",
            project=_make_project(),
            timeout_minutes=5,
            continue_session=True,
            output_format="json",
        )

        command = docker_client.containers.run.call_args.kwargs["command"]
        assert isinstance(command, list)
        assert command[0] == "sh"
        assert "--continue" in command
        assert "--output-format" in command
        assert "json" in command


class TestRunHooks:
    def test_runs_each_command_separately(self):
        container1 = _mock_container(exit_code=0, logs=b"synced")
        container2 = _mock_container(exit_code=0, logs=b"passed")
        docker_client = MagicMock()
        docker_client.containers.run.side_effect = [container1, container2]
        runner = DockerRunner(docker_client, cache_volume="test-cache")

        result = runner.run_hooks(
            work_dir="work-vol",
            commands=["uv sync", "uv run pytest"],
            project=_make_project(),
        )

        assert docker_client.containers.run.call_count == 2
        assert result.exit_code == 0

    def test_stops_on_first_failure(self):
        container1 = _mock_container(exit_code=1, logs=b"sync failed")
        docker_client = _mock_docker_client(container1)
        runner = DockerRunner(docker_client, cache_volume="test-cache")

        result = runner.run_hooks(
            work_dir="/w",
            commands=["uv sync", "uv run pytest"],
            project=_make_project(),
        )

        # Only first command ran
        assert docker_client.containers.run.call_count == 1
        assert result.exit_code == 1
        assert result.logs == "sync failed"

    def test_empty_commands_returns_success(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(docker_client, cache_volume="test-cache")

        result = runner.run_hooks(
            work_dir="/w",
            commands=[],
            project=_make_project(),
        )

        assert result.exit_code == 0
        docker_client.containers.run.assert_not_called()


class TestKillStaleContainers:
    def test_kills_and_removes_labeled_containers(self):
        container1 = MagicMock()
        container2 = MagicMock()
        docker_client = MagicMock()
        docker_client.containers.list.return_value = [container1, container2]
        docker_client.volumes.list.return_value = []
        runner = DockerRunner(docker_client, cache_volume="test-cache")

        runner.kill_stale_containers()

        docker_client.containers.list.assert_called_once_with(
            filters={"label": "wowkmang"}
        )
        container1.kill.assert_called_once()
        container1.remove.assert_called_once()
        container2.kill.assert_called_once()
        container2.remove.assert_called_once()

    def test_ignores_errors_during_cleanup(self):
        container = MagicMock()
        container.kill.side_effect = Exception("already dead")
        docker_client = MagicMock()
        docker_client.containers.list.return_value = [container]
        docker_client.volumes.list.return_value = []
        runner = DockerRunner(docker_client, cache_volume="test-cache")

        # Should not raise
        runner.kill_stale_containers()

    def test_removes_orphaned_volumes(self):
        vol1 = MagicMock()
        vol2 = MagicMock()
        docker_client = MagicMock()
        docker_client.containers.list.return_value = []
        docker_client.volumes.list.return_value = [vol1, vol2]
        runner = DockerRunner(docker_client, cache_volume="test-cache")

        runner.kill_stale_containers()

        docker_client.volumes.list.assert_called_once_with(
            filters={"label": "wowkmang"}
        )
        vol1.remove.assert_called_once()
        vol2.remove.assert_called_once()


class TestCreateVolume:
    def test_creates_volume_with_label(self):
        docker_client = MagicMock()
        runner = DockerRunner(docker_client, cache_volume="test-cache")

        name = runner.create_volume(prefix="wowkmang-work")

        assert name.startswith("wowkmang-work-")
        assert len(name) == len("wowkmang-work-") + 12
        docker_client.volumes.create.assert_called_once_with(
            name=name, labels={"wowkmang": "true"}
        )


class TestRemoveVolume:
    def test_removes_volume(self):
        docker_client = MagicMock()
        runner = DockerRunner(docker_client, cache_volume="test-cache")

        runner.remove_volume("wowkmang-work-abc123")

        docker_client.volumes.get.assert_called_once_with("wowkmang-work-abc123")
        docker_client.volumes.get.return_value.remove.assert_called_once()

    def test_ignores_error(self):
        docker_client = MagicMock()
        docker_client.volumes.get.side_effect = Exception("not found")
        runner = DockerRunner(docker_client, cache_volume="test-cache")

        # Should not raise
        runner.remove_volume("nonexistent")


class TestRunGit:
    def test_runs_command_in_container(self):
        container = _mock_container(exit_code=0, logs=b"git output")
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client, cache_volume="my-cache")

        result = runner.run_git(
            command="git status",
            image="ghcr.io/org/image:latest",
            work_volume="work-vol-123",
            environment={"GIT_TERMINAL_PROMPT": "0"},
        )

        assert result.exit_code == 0
        assert result.logs == "git output"
        kwargs = docker_client.containers.run.call_args.kwargs
        assert kwargs["command"] == [
            "sh",
            "-c",
            "git config --global --add safe.directory '*' && git status",
        ]
        assert kwargs["volumes"]["work-vol-123"]["bind"] == "/workspace"
        assert kwargs["volumes"]["my-cache"]["bind"] == "/cache"
        assert kwargs["environment"]["GIT_TERMINAL_PROMPT"] == "0"

    def test_default_environment(self):
        container = _mock_container()
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client, cache_volume="test-cache")

        runner.run_git(
            command="git status",
            image="img",
            work_volume="vol",
        )

        kwargs = docker_client.containers.run.call_args.kwargs
        assert kwargs["environment"] == {}


class TestSeedVolume:
    def test_copies_files_into_volume(self):
        container = _mock_container(exit_code=0, logs=b"")
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client, cache_volume="test-cache")

        result = runner.seed_volume(
            image="img:latest",
            source_host_path="/home/user/.claude",
            target_volume="session-vol",
            target_path="/target",
        )

        assert result.exit_code == 0
        kwargs = docker_client.containers.run.call_args.kwargs
        assert kwargs["command"] == ["cp", "-a", "/source/.", "/target/"]
        assert kwargs["volumes"]["/home/user/.claude"]["bind"] == "/source"
        assert kwargs["volumes"]["/home/user/.claude"]["mode"] == "ro"
        assert kwargs["volumes"]["session-vol"]["bind"] == "/target"
        assert kwargs["volumes"]["session-vol"]["mode"] == "rw"


class TestEnsureImage:
    def test_pulls_with_pull_token(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(
            docker_client, cache_volume="test-cache", pull_token="ghp_global"
        )
        project = _make_project()

        runner.ensure_image("ghcr.io/org/image:latest", project)

        docker_client.images.pull.assert_called_once_with(
            "ghcr.io/org/image:latest",
            auth_config={"username": "x", "password": "ghp_global"},
        )

    def test_falls_back_to_project_token(self):
        docker_client = _mock_docker_client()
        docker_client.images.pull.side_effect = [
            Exception("auth failed"),  # pull_token fails
            MagicMock(),  # project token succeeds
        ]
        runner = DockerRunner(
            docker_client, cache_volume="test-cache", pull_token="ghp_global"
        )
        project = _make_project(credentials={"GITHUB_TOKEN": "ghp_project"})

        runner.ensure_image("ghcr.io/org/image:latest", project)

        assert docker_client.images.pull.call_count == 2
        second_call = docker_client.images.pull.call_args_list[1]
        assert second_call.kwargs["auth_config"]["password"] == "ghp_project"

    def test_falls_back_to_unauthenticated(self):
        docker_client = _mock_docker_client()
        docker_client.images.pull.side_effect = [
            Exception("auth failed"),  # pull_token fails
            Exception("auth failed"),  # project token fails
            MagicMock(),  # unauthenticated succeeds
        ]
        runner = DockerRunner(
            docker_client, cache_volume="test-cache", pull_token="ghp_global"
        )
        project = _make_project(credentials={"GITHUB_TOKEN": "ghp_project"})

        runner.ensure_image("ghcr.io/org/image:latest", project)

        assert docker_client.images.pull.call_count == 3
        # Third call has no auth_config
        third_call = docker_client.images.pull.call_args_list[2]
        assert "auth_config" not in third_call.kwargs

    def test_skips_duplicate_project_token(self):
        """When project token equals pull_token, don't try it twice."""
        docker_client = _mock_docker_client()
        docker_client.images.pull.side_effect = [
            Exception("auth failed"),  # pull_token fails
            MagicMock(),  # unauthenticated succeeds
        ]
        runner = DockerRunner(
            docker_client, cache_volume="test-cache", pull_token="ghp_same"
        )
        project = _make_project(credentials={"GITHUB_TOKEN": "ghp_same"})

        runner.ensure_image("ghcr.io/org/image:latest", project)

        # Should skip project token (same as pull_token) and go to unauthenticated
        assert docker_client.images.pull.call_count == 2

    def test_caches_pulled_images(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(
            docker_client, cache_volume="test-cache", pull_token="ghp_global"
        )
        project = _make_project()

        runner.ensure_image("ghcr.io/org/image:latest", project)
        runner.ensure_image("ghcr.io/org/image:latest", project)

        # Only pulled once
        docker_client.images.pull.assert_called_once()

    def test_no_pull_token_uses_project_token(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(docker_client, cache_volume="test-cache")
        project = _make_project(credentials={"GITHUB_TOKEN": "ghp_project"})

        runner.ensure_image("ghcr.io/org/image:latest", project)

        docker_client.images.pull.assert_called_once_with(
            "ghcr.io/org/image:latest",
            auth_config={"username": "x", "password": "ghp_project"},
        )

    def test_all_auth_fails_does_not_raise(self):
        """When all pull attempts fail, ensure_image should not raise."""
        docker_client = _mock_docker_client()
        docker_client.images.pull.side_effect = Exception("denied")
        runner = DockerRunner(
            docker_client, cache_volume="test-cache", pull_token="ghp_global"
        )
        project = _make_project()

        # Should not raise — will try to use local image
        runner.ensure_image("ghcr.io/org/image:latest", project)

    def test_run_claude_code_does_not_pull(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(
            docker_client, cache_volume="test-cache", pull_token="ghp_tok"
        )
        project = _make_project()

        runner.run_claude_code(
            work_dir="/w",
            task_prompt="t",
            model="s",
            project=project,
            timeout_minutes=1,
        )

        docker_client.images.pull.assert_not_called()

    def test_run_hooks_does_not_pull(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(
            docker_client, cache_volume="test-cache", pull_token="ghp_tok"
        )
        project = _make_project()

        runner.run_hooks(
            work_dir="/w",
            commands=["echo hi"],
            project=project,
        )

        docker_client.images.pull.assert_not_called()


class TestCopyToWorkdir:
    def test_copies_cache_into_workdir(self):
        container = _mock_container(exit_code=0, logs=b"copied")
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client, cache_volume="my-cache")

        result = runner.copy_to_workdir(
            work_volume="work-vol",
            cache_subdir="github.com_user_project",
            image="img:latest",
        )

        assert result.exit_code == 0
        assert result.logs == "copied"
        kwargs = docker_client.containers.run.call_args.kwargs
        assert kwargs["volumes"]["work-vol"]["bind"] == "/workspace"
        assert kwargs["volumes"]["work-vol"]["mode"] == "rw"
        assert kwargs["volumes"]["my-cache"]["bind"] == "/cache"
        assert kwargs["volumes"]["my-cache"]["mode"] == "ro"

    def test_command_contains_cache_subdir(self):
        container = _mock_container()
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client, cache_volume="my-cache")

        runner.copy_to_workdir(
            work_volume="work-vol",
            cache_subdir="github.com_org_repo",
            image="img",
        )

        command = docker_client.containers.run.call_args.kwargs["command"]
        assert command[0] == "sh"
        assert command[1] == "-c"
        assert "github.com_org_repo" in command[2]
        assert "/workspace/.cache/" in command[2]

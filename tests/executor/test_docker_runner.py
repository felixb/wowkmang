from unittest.mock import MagicMock, patch

import pytest

from wowkmang.api.config import ProjectConfig
from wowkmang.executor.docker_runner import ContainerResult, DockerRunner


def _make_project(**overrides) -> ProjectConfig:
    defaults = {
        "name": "test",
        "repo": "https://github.com/u/p",
        "github_token": "ghp_secret",
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
        runner = DockerRunner(docker_client)
        project = _make_project()

        result = runner.run_claude_code(
            work_dir="work-vol-123",
            project_volume="proj-vol",
            task_prompt="Fix the bug",
            model="sonnet",
            project=project,
            timeout_minutes=30,
        )

        docker_client.containers.run.assert_called_once()
        kwargs = docker_client.containers.run.call_args.kwargs
        assert kwargs["image"] == runner.resolve_image(project)
        assert kwargs["working_dir"] == "/workspace/repo"
        assert kwargs["detach"] is True
        assert kwargs["mem_limit"] == "4g"
        assert kwargs["environment"]["CLAUDE_MODEL"] == "sonnet"
        assert kwargs["environment"]["GITHUB_TOKEN"] == "ghp_secret"
        assert kwargs["environment"]["HOME"] == "/cache"
        assert kwargs["entrypoint"] == ""
        assert kwargs["user"] == "1000:1000"
        assert "work-vol-123" in kwargs["volumes"]

    def test_returns_container_result(self):
        container = _mock_container(exit_code=0, logs=b"all done")
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client)

        result = runner.run_claude_code(
            work_dir="work-vol",
            project_volume="proj-vol",
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
        runner = DockerRunner(docker_client)

        result = runner.run_claude_code(
            work_dir="/w",
            project_volume="proj-vol",
            task_prompt="task",
            model="sonnet",
            project=_make_project(),
            timeout_minutes=10,
        )

        assert result.exit_code == 1
        assert result.logs == "error occurred"

    def test_command_is_list_with_no_bootstrap(self):
        container = _mock_container()
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client)

        runner.run_claude_code(
            work_dir="/w",
            project_volume="proj-vol",
            task_prompt="Fix the bug",
            model="sonnet",
            project=_make_project(),
            timeout_minutes=10,
        )

        command = docker_client.containers.run.call_args.kwargs["command"]
        assert isinstance(command, list)
        assert command[0] == "claude"
        assert "--model" in command
        assert "sonnet" in command
        assert "Fix the bug" in command
        # No bootstrap script
        assert "mkdir" not in " ".join(str(c) for c in command)
        assert ".claude-config" not in " ".join(str(c) for c in command)

    def test_extra_instructions_prepended_to_prompt(self):
        container = _mock_container()
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client)
        project = _make_project(extra_instructions="Always write tests.")

        runner.run_claude_code(
            work_dir="/w",
            project_volume="proj-vol",
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
        runner = DockerRunner(docker_client)

        with pytest.raises(Exception, match="timeout"):
            runner.run_claude_code(
                work_dir="/w",
                project_volume="proj-vol",
                task_prompt="task",
                model="sonnet",
                project=_make_project(),
                timeout_minutes=1,
            )

        container.kill.assert_called_once()

    def test_container_removed_on_success(self):
        container = _mock_container()
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client)

        runner.run_claude_code(
            work_dir="/w",
            project_volume="proj-vol",
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
        runner = DockerRunner(docker_client)

        with pytest.raises(RuntimeError):
            runner.run_claude_code(
                work_dir="/w",
                project_volume="proj-vol",
                task_prompt="t",
                model="s",
                project=_make_project(),
                timeout_minutes=1,
            )

        container.remove.assert_called_once()

    def test_project_volume_mounted_as_cache(self):
        container = _mock_container()
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client)

        runner.run_claude_code(
            work_dir="work-vol",
            project_volume="my-project-vol",
            task_prompt="t",
            model="s",
            project=_make_project(),
            timeout_minutes=1,
        )

        volumes = docker_client.containers.run.call_args.kwargs["volumes"]
        assert "my-project-vol" in volumes
        assert volumes["my-project-vol"]["bind"] == "/cache"

    def test_container_labeled(self):
        container = _mock_container()
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client)

        runner.run_claude_code(
            work_dir="/w",
            project_volume="proj-vol",
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
        runner = DockerRunner(docker_client)

        runner.run_claude_code(
            work_dir="/w",
            project_volume="proj-vol",
            task_prompt="summarize",
            model="haiku",
            project=_make_project(),
            timeout_minutes=5,
            continue_session=True,
            output_format="json",
        )

        command = docker_client.containers.run.call_args.kwargs["command"]
        assert isinstance(command, list)
        assert command[0] == "claude"
        assert "--continue" in command
        assert "--output-format" in command
        assert "json" in command


class TestRunHooks:
    def test_runs_each_command_separately(self):
        container1 = _mock_container(exit_code=0, logs=b"synced")
        container2 = _mock_container(exit_code=0, logs=b"passed")
        docker_client = MagicMock()
        docker_client.containers.run.side_effect = [container1, container2]
        runner = DockerRunner(docker_client)

        result = runner.run_hooks(
            work_dir="work-vol",
            project_volume="proj-vol",
            commands=["uv sync", "uv run pytest"],
            project=_make_project(),
        )

        assert docker_client.containers.run.call_count == 2
        assert result.exit_code == 0

    def test_stops_on_first_failure(self):
        container1 = _mock_container(exit_code=1, logs=b"sync failed")
        docker_client = _mock_docker_client(container1)
        runner = DockerRunner(docker_client)

        result = runner.run_hooks(
            work_dir="/w",
            project_volume="proj-vol",
            commands=["uv sync", "uv run pytest"],
            project=_make_project(),
        )

        # Only first command ran
        assert docker_client.containers.run.call_count == 1
        assert result.exit_code == 1
        assert result.logs == "sync failed"

    def test_empty_commands_returns_success(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(docker_client)

        result = runner.run_hooks(
            work_dir="/w",
            project_volume="proj-vol",
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
        runner = DockerRunner(docker_client)

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
        runner = DockerRunner(docker_client)

        # Should not raise
        runner.kill_stale_containers()

    def test_removes_orphaned_volumes(self):
        vol1 = MagicMock()
        vol2 = MagicMock()
        docker_client = MagicMock()
        docker_client.containers.list.return_value = []
        docker_client.volumes.list.return_value = [vol1, vol2]
        runner = DockerRunner(docker_client)

        runner.kill_stale_containers()

        docker_client.volumes.list.assert_called_once_with(
            filters={"label": "wowkmang"}
        )
        vol1.remove.assert_called_once()
        vol2.remove.assert_called_once()


class TestCreateVolume:
    def test_creates_volume_with_label(self):
        docker_client = MagicMock()
        runner = DockerRunner(docker_client)

        name = runner.create_volume(prefix="wowkmang-work")

        assert name.startswith("wowkmang-work-")
        assert len(name) == len("wowkmang-work-") + 12
        docker_client.volumes.create.assert_called_once_with(
            name=name, labels={"wowkmang": "true"}
        )


class TestEnsureProjectVolume:
    def test_creates_volume_without_wowkmang_label(self):
        docker_client = MagicMock()
        docker_client.volumes.get.side_effect = Exception("not found")
        runner = DockerRunner(docker_client)

        name = runner.ensure_project_volume("myproject")

        assert name == "wowkmang-project-myproject"
        docker_client.volumes.create.assert_called_once_with(
            name="wowkmang-project-myproject"
        )
        # No wowkmang label so it won't be cleaned up by kill_stale_containers
        call_kwargs = docker_client.volumes.create.call_args
        assert "labels" not in call_kwargs.kwargs

    def test_reuses_existing_volume(self):
        docker_client = MagicMock()
        existing_vol = MagicMock()
        docker_client.volumes.get.return_value = existing_vol
        runner = DockerRunner(docker_client)

        name = runner.ensure_project_volume("myproject")

        assert name == "wowkmang-project-myproject"
        docker_client.volumes.get.assert_called_once_with("wowkmang-project-myproject")
        docker_client.volumes.create.assert_not_called()

    def test_project_volume_not_deleted_by_kill_stale(self):
        """ensure_project_volume must not add the wowkmang label."""
        docker_client = MagicMock()
        docker_client.volumes.get.side_effect = Exception("not found")
        runner = DockerRunner(docker_client)

        runner.ensure_project_volume("proj")

        create_call = docker_client.volumes.create.call_args
        # Either no kwargs or no labels key
        kwargs = create_call.kwargs if create_call.kwargs else {}
        args_dict = create_call.args[0] if create_call.args else {}
        assert "labels" not in kwargs
        assert "labels" not in args_dict


class TestRemoveVolume:
    def test_removes_volume(self):
        docker_client = MagicMock()
        runner = DockerRunner(docker_client)

        runner.remove_volume("wowkmang-work-abc123")

        docker_client.volumes.get.assert_called_once_with("wowkmang-work-abc123")
        docker_client.volumes.get.return_value.remove.assert_called_once()

    def test_ignores_error(self):
        docker_client = MagicMock()
        docker_client.volumes.get.side_effect = Exception("not found")
        runner = DockerRunner(docker_client)

        # Should not raise
        runner.remove_volume("nonexistent")


class TestSeedCredentials:
    def test_copies_credentials_into_project_volume(self):
        container = _mock_container(exit_code=0, logs=b"")
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client)

        result = runner.seed_credentials(
            image="img:latest",
            source_dir="/home/user/.claude",
            project_volume="proj-vol",
        )

        assert result.exit_code == 0
        kwargs = docker_client.containers.run.call_args.kwargs
        assert kwargs["volumes"]["/home/user/.claude"]["bind"] == "/source"
        assert kwargs["volumes"]["/home/user/.claude"]["mode"] == "ro"
        assert kwargs["volumes"]["proj-vol"]["bind"] == "/cache"
        assert kwargs["volumes"]["proj-vol"]["mode"] == "rw"
        # Copies only credentials.json
        script = kwargs["command"][2]
        assert "credentials.json" in script
        assert "mkdir -p /cache/.claude" in script
        assert "cp -a /source/.credentials.json /cache/.claude/" in script
        # Runs as root (no user)
        assert "user" not in kwargs


class TestChownVolume:
    def test_chowns_workspace(self):
        container = _mock_container(exit_code=0, logs=b"")
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client)

        result = runner.chown_volume(
            image="img:latest",
            work_volume="work-vol",
            uid="1000:1000",
        )

        assert result.exit_code == 0
        kwargs = docker_client.containers.run.call_args.kwargs
        script = kwargs["command"][2]
        assert "chown -R 1000:1000 /workspace" in script
        # No longer creates .home
        assert "mkdir -p /workspace/.home" not in script
        assert kwargs["volumes"]["work-vol"]["bind"] == "/workspace"
        assert "user" not in kwargs  # runs as root

    def test_custom_uid(self):
        container = _mock_container()
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client)

        runner.chown_volume(
            image="img",
            work_volume="vol",
            uid="2000:2000",
        )

        kwargs = docker_client.containers.run.call_args.kwargs
        script = kwargs["command"][2]
        assert "chown -R 2000:2000 /workspace" in script


class TestChownProjectVolume:
    def test_creates_and_chowns_cache(self):
        container = _mock_container(exit_code=0, logs=b"")
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client)

        result = runner.chown_project_volume(
            image="img:latest",
            project_volume="proj-vol",
            uid="1000:1000",
        )

        assert result.exit_code == 0
        kwargs = docker_client.containers.run.call_args.kwargs
        assert kwargs["volumes"]["proj-vol"]["bind"] == "/cache"
        assert "user" not in kwargs  # runs as root
        script = kwargs["command"][2]
        assert "mkdir -p /cache" in script
        assert "chown -R 1000:1000 /cache" in script

    def test_custom_uid(self):
        container = _mock_container()
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client)

        runner.chown_project_volume(
            image="img",
            project_volume="proj-vol",
            uid="2000:2000",
        )

        script = docker_client.containers.run.call_args.kwargs["command"][2]
        assert "chown -R 2000:2000 /cache" in script


class TestSetupNetrc:
    def test_writes_netrc_with_token(self):
        container = _mock_container(exit_code=0, logs=b"")
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client)

        runner.setup_netrc(
            project_volume="proj-vol",
            image="img:latest",
            github_token="ghp_secret",
            uid="1000:1000",
        )

        docker_client.containers.run.assert_called_once()
        kwargs = docker_client.containers.run.call_args.kwargs
        assert kwargs["volumes"]["proj-vol"]["bind"] == "/cache"
        # Token is passed via env var, not in the command
        assert kwargs["environment"]["_NETRC_TOKEN"] == "ghp_secret"
        script = kwargs["command"][2]
        assert ".netrc" in script
        assert "chmod 600" in script
        # Token must NOT appear in the command itself
        assert "ghp_secret" not in script

    def test_skips_when_no_token(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(docker_client)

        runner.setup_netrc(
            project_volume="proj-vol",
            image="img",
            github_token="",
        )

        docker_client.containers.run.assert_not_called()


class TestValidateUid:
    def test_valid_uid(self):
        assert DockerRunner._validate_uid("1000:1000") == "1000:1000"
        assert DockerRunner._validate_uid("0:0") == "0:0"

    def test_rejects_injection(self):
        with pytest.raises(ValueError, match="Invalid container UID format"):
            DockerRunner._validate_uid("1000:1000 /etc && rm -rf /")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="Invalid container UID format"):
            DockerRunner._validate_uid("")

    def test_rejects_no_colon(self):
        with pytest.raises(ValueError, match="Invalid container UID format"):
            DockerRunner._validate_uid("1000")

    def test_chown_volume_rejects_bad_uid(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(docker_client)

        with pytest.raises(ValueError, match="Invalid container UID format"):
            runner.chown_volume(image="img", work_volume="vol", uid="; rm -rf /")

    def test_chown_project_volume_rejects_bad_uid(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(docker_client)

        with pytest.raises(ValueError, match="Invalid container UID format"):
            runner.chown_project_volume(
                image="img", project_volume="vol", uid="$(evil)"
            )


class TestRunCommand:
    def test_passes_user_to_container(self):
        container = _mock_container()
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client)

        runner.run_command(
            work_dir="work-vol",
            project_volume="proj-vol",
            command=["echo", "hi"],
            image="img",
        )

        kwargs = docker_client.containers.run.call_args.kwargs
        assert kwargs["user"] == "1000:1000"
        assert kwargs["environment"]["HOME"] == "/cache"

    def test_custom_default_uid(self):
        container = _mock_container()
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client, default_uid="2000:2000")

        runner.run_command(
            work_dir="work-vol",
            project_volume="proj-vol",
            command=["echo", "hi"],
            image="img",
        )

        kwargs = docker_client.containers.run.call_args.kwargs
        assert kwargs["user"] == "2000:2000"

    def test_project_volume_mounted_as_cache(self):
        container = _mock_container()
        docker_client = _mock_docker_client(container)
        runner = DockerRunner(docker_client)

        runner.run_command(
            work_dir="work-vol",
            project_volume="my-proj-vol",
            command=["echo", "hi"],
            image="img",
        )

        kwargs = docker_client.containers.run.call_args.kwargs
        assert "my-proj-vol" in kwargs["volumes"]
        assert kwargs["volumes"]["my-proj-vol"]["bind"] == "/cache"


class TestResolveImage:
    def test_uses_project_image_when_set(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(
            docker_client, default_docker_image="ghcr.io/global/image:latest"
        )
        project = _make_project(docker_image="ghcr.io/project/image:v2")

        assert runner.resolve_image(project) == "ghcr.io/project/image:v2"

    def test_falls_back_to_default_when_project_image_empty(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(
            docker_client, default_docker_image="ghcr.io/global/image:latest"
        )
        project = _make_project()  # docker_image="" by default

        assert runner.resolve_image(project) == "ghcr.io/global/image:latest"

    def test_run_claude_code_uses_resolved_image(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(
            docker_client, default_docker_image="ghcr.io/global/image:latest"
        )
        project = _make_project()  # no docker_image set

        runner.run_claude_code(
            work_dir="work-vol",
            project_volume="proj-vol",
            task_prompt="do stuff",
            model="sonnet",
            project=project,
            timeout_minutes=30,
        )

        kwargs = docker_client.containers.run.call_args.kwargs
        assert kwargs["image"] == "ghcr.io/global/image:latest"

    def test_run_claude_code_uses_project_image_over_default(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(
            docker_client, default_docker_image="ghcr.io/global/image:latest"
        )
        project = _make_project(docker_image="ghcr.io/project/custom:v1")

        runner.run_claude_code(
            work_dir="work-vol",
            project_volume="proj-vol",
            task_prompt="do stuff",
            model="sonnet",
            project=project,
            timeout_minutes=30,
        )

        kwargs = docker_client.containers.run.call_args.kwargs
        assert kwargs["image"] == "ghcr.io/project/custom:v1"


class TestEnsureImage:
    def test_pulls_with_pull_token(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(docker_client, pull_token="ghp_global")
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
        runner = DockerRunner(docker_client, pull_token="ghp_global")
        project = _make_project(github_token="ghp_project")

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
        runner = DockerRunner(docker_client, pull_token="ghp_global")
        project = _make_project(github_token="ghp_project")

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
        runner = DockerRunner(docker_client, pull_token="ghp_same")
        project = _make_project(github_token="ghp_same")

        runner.ensure_image("ghcr.io/org/image:latest", project)

        # Should skip project token (same as pull_token) and go to unauthenticated
        assert docker_client.images.pull.call_count == 2

    def test_caches_pulled_images(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(docker_client, pull_token="ghp_global")
        project = _make_project()

        runner.ensure_image("ghcr.io/org/image:latest", project)
        runner.ensure_image("ghcr.io/org/image:latest", project)

        # Only pulled once
        docker_client.images.pull.assert_called_once()

    def test_no_pull_token_uses_project_token(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(docker_client)
        project = _make_project(github_token="ghp_project")

        runner.ensure_image("ghcr.io/org/image:latest", project)

        docker_client.images.pull.assert_called_once_with(
            "ghcr.io/org/image:latest",
            auth_config={"username": "x", "password": "ghp_project"},
        )

    def test_uses_project_github_token_field(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(docker_client)
        project = _make_project(github_token="ghp_field")

        runner.ensure_image("ghcr.io/org/image:latest", project)

        docker_client.images.pull.assert_called_once_with(
            "ghcr.io/org/image:latest",
            auth_config={"username": "x", "password": "ghp_field"},
        )

    def test_falls_back_to_global_github_token(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(docker_client, github_token="ghp_global")
        project = _make_project(github_token="")

        runner.ensure_image("ghcr.io/org/image:latest", project)

        docker_client.images.pull.assert_called_once_with(
            "ghcr.io/org/image:latest",
            auth_config={"username": "x", "password": "ghp_global"},
        )

    def test_all_auth_fails_does_not_raise(self):
        """When all pull attempts fail, ensure_image should not raise."""
        docker_client = _mock_docker_client()
        docker_client.images.pull.side_effect = Exception("denied")
        runner = DockerRunner(docker_client, pull_token="ghp_global")
        project = _make_project()

        # Should not raise — will try to use local image
        runner.ensure_image("ghcr.io/org/image:latest", project)

    def test_run_claude_code_does_not_pull(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(docker_client, pull_token="ghp_tok")
        project = _make_project()

        runner.run_claude_code(
            work_dir="/w",
            project_volume="proj-vol",
            task_prompt="t",
            model="s",
            project=project,
            timeout_minutes=1,
        )

        docker_client.images.pull.assert_not_called()

    def test_run_hooks_does_not_pull(self):
        docker_client = _mock_docker_client()
        runner = DockerRunner(docker_client, pull_token="ghp_tok")
        project = _make_project()

        runner.run_hooks(
            work_dir="/w",
            project_volume="proj-vol",
            commands=["echo hi"],
            project=project,
        )

        docker_client.images.pull.assert_not_called()

from unittest.mock import MagicMock, call

import pytest

from wowkmang.config import ProjectConfig
from wowkmang.docker_runner import ContainerResult
from wowkmang.hooks import FixLoop, HookResult, HookRunner
from wowkmang.models import Task, TaskSource, TaskSourceInfo


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


def _mock_docker_runner(hook_exit_codes: list[int] | None = None):
    runner = MagicMock()

    if hook_exit_codes is not None:
        hook_results = [
            ContainerResult(exit_code=code, logs=f"output_{i}")
            for i, code in enumerate(hook_exit_codes)
        ]
        runner.run_hooks.side_effect = hook_results

    runner.run_claude_code.return_value = ContainerResult(exit_code=0, logs="fixed")
    return runner


class TestHookRunner:
    def test_hooks_success(self):
        docker_runner = _mock_docker_runner()
        docker_runner.run_hooks.return_value = ContainerResult(exit_code=0, logs="ok")
        hook_runner = HookRunner(docker_runner)
        project = _make_project()

        result = hook_runner.run_hooks(["uv sync"], "/work", project)

        assert result.success is True
        assert result.exit_code == 0
        assert result.output == "ok"

    def test_hooks_failure(self):
        docker_runner = _mock_docker_runner()
        docker_runner.run_hooks.return_value = ContainerResult(
            exit_code=1, logs="install failed"
        )
        hook_runner = HookRunner(docker_runner)
        project = _make_project()

        result = hook_runner.run_hooks(["uv sync"], "/work", project)

        assert result.success is False
        assert result.exit_code == 1
        assert result.output == "install failed"


class TestFixLoop:
    def test_fix_succeeds_on_first_attempt(self):
        # Hook fails initially, then succeeds after fix
        docker_runner = _mock_docker_runner(hook_exit_codes=[0])
        hook_runner = HookRunner(docker_runner)
        fix_loop = FixLoop(docker_runner, hook_runner)
        project = _make_project(max_fix_attempts=2)
        task = _make_task()
        initial_failure = HookResult(success=False, output="tests failed", exit_code=1)

        result = fix_loop.run(task, project, "/work", initial_failure)

        assert result.success is True
        docker_runner.run_claude_code.assert_called_once()
        assert (
            "tests failed"
            in docker_runner.run_claude_code.call_args.kwargs["task_prompt"]
        )

    def test_fix_succeeds_on_second_attempt(self):
        # Hook fails on first retry, succeeds on second
        docker_runner = _mock_docker_runner(hook_exit_codes=[1, 0])
        hook_runner = HookRunner(docker_runner)
        fix_loop = FixLoop(docker_runner, hook_runner)
        project = _make_project(max_fix_attempts=2)
        task = _make_task()
        initial_failure = HookResult(success=False, output="fail", exit_code=1)

        result = fix_loop.run(task, project, "/work", initial_failure)

        assert result.success is True
        assert docker_runner.run_claude_code.call_count == 2

    def test_fix_exhausts_attempts_and_fails(self):
        # Hook keeps failing
        docker_runner = _mock_docker_runner(hook_exit_codes=[1, 1])
        hook_runner = HookRunner(docker_runner)
        fix_loop = FixLoop(docker_runner, hook_runner)
        project = _make_project(max_fix_attempts=2)
        task = _make_task()
        initial_failure = HookResult(success=False, output="fail", exit_code=1)

        result = fix_loop.run(task, project, "/work", initial_failure)

        assert result.success is False
        assert docker_runner.run_claude_code.call_count == 2

    def test_fix_stops_early_on_success(self):
        docker_runner = _mock_docker_runner(hook_exit_codes=[0])
        hook_runner = HookRunner(docker_runner)
        fix_loop = FixLoop(docker_runner, hook_runner)
        project = _make_project(max_fix_attempts=5)
        task = _make_task()
        initial_failure = HookResult(success=False, output="fail", exit_code=1)

        result = fix_loop.run(task, project, "/work", initial_failure)

        assert result.success is True
        # Only one attempt needed
        assert docker_runner.run_claude_code.call_count == 1

    def test_fix_uses_task_model_override(self):
        docker_runner = _mock_docker_runner(hook_exit_codes=[0])
        hook_runner = HookRunner(docker_runner)
        fix_loop = FixLoop(docker_runner, hook_runner)
        project = _make_project()
        task = _make_task(model="opus")
        initial_failure = HookResult(success=False, output="fail", exit_code=1)

        fix_loop.run(task, project, "/work", initial_failure)

        assert docker_runner.run_claude_code.call_args.kwargs["model"] == "opus"

    def test_fix_uses_project_default_model_when_no_override(self):
        docker_runner = _mock_docker_runner(hook_exit_codes=[0])
        hook_runner = HookRunner(docker_runner)
        fix_loop = FixLoop(docker_runner, hook_runner)
        project = _make_project(default_model="haiku")
        task = _make_task()
        initial_failure = HookResult(success=False, output="fail", exit_code=1)

        fix_loop.run(task, project, "/work", initial_failure)

        assert docker_runner.run_claude_code.call_args.kwargs["model"] == "haiku"

import json
from unittest.mock import MagicMock

import pytest

from wowkmang.api.config import ProjectConfig
from wowkmang.executor.docker_runner import ContainerResult
from wowkmang.taskqueue.models import Task, TaskSource, TaskSourceInfo
from wowkmang.executor.summary import (
    PRMetadata,
    SummaryGenerator,
    _extract_yaml,
    _fallback_metadata,
    _parse_response,
)


def _make_task(**overrides) -> Task:
    defaults = {
        "project": "myproject",
        "repo": "https://github.com/user/project",
        "task": "Fix the login bug",
        "source": TaskSourceInfo(type=TaskSource.GITHUB_ISSUE, issue_number=42),
    }
    defaults.update(overrides)
    return Task(**defaults)


def _make_project(**overrides) -> ProjectConfig:
    defaults = {"name": "test", "repo": "https://github.com/u/p"}
    defaults.update(overrides)
    return ProjectConfig(**defaults)


VALID_YAML = """title: Fix login validation
branch: fix-login-validation-42
description: |
  Fix the login bug by adding proper validation.

  Closes #42
"""


def _mock_docker_runner(yaml_response: str) -> MagicMock:
    """Create a mock docker runner that returns Claude Code JSON output with YAML."""
    runner = MagicMock()
    # Claude Code --output-format json wraps result in {"result": "..."}
    json_output = json.dumps({"result": f"```yaml\n{yaml_response}\n```"})
    runner.run_claude_code.return_value = ContainerResult(exit_code=0, logs=json_output)
    return runner


class TestGenerate:
    def test_happy_path(self):
        docker_runner = _mock_docker_runner(VALID_YAML)
        gen = SummaryGenerator(docker_runner)
        task = _make_task()
        project = _make_project()

        result = gen.generate(
            task,
            "diff content here",
            project=project,
            work_dir="/work",
            project_volume="proj-vol",
        )

        assert isinstance(result, PRMetadata)
        assert result.title == "Fix login validation"
        assert result.branch == "wowkmang/fix-login-validation-42"
        assert "Closes #42" in result.description
        # Verify claude code was called with --continue and haiku
        docker_runner.run_claude_code.assert_called_once()
        call_kwargs = docker_runner.run_claude_code.call_args.kwargs
        assert call_kwargs["model"] == "haiku"
        assert call_kwargs["continue_session"] is True
        assert call_kwargs["output_format"] == "json"
        assert call_kwargs["project_volume"] == "proj-vol"

    def test_diff_truncation(self):
        docker_runner = _mock_docker_runner(VALID_YAML)
        gen = SummaryGenerator(docker_runner)
        task = _make_task()
        long_diff = "x" * 20000

        gen.generate(
            task,
            long_diff,
            project=_make_project(),
            work_dir="/w",
        )

        prompt = docker_runner.run_claude_code.call_args.kwargs["task_prompt"]
        assert "x" * 10000 in prompt
        assert "x" * 10001 not in prompt

    def test_hook_failure_included_in_prompt(self):
        docker_runner = _mock_docker_runner(VALID_YAML)
        gen = SummaryGenerator(docker_runner)
        task = _make_task()

        gen.generate(
            task,
            "diff",
            hook_output="FAILED test_login.py::test_bad",
            project=_make_project(),
            work_dir="/w",
        )

        prompt = docker_runner.run_claude_code.call_args.kwargs["task_prompt"]
        assert "Post-task hooks FAILED" in prompt
        assert "FAILED test_login.py::test_bad" in prompt

    def test_no_hook_failure_shows_passed(self):
        docker_runner = _mock_docker_runner(VALID_YAML)
        gen = SummaryGenerator(docker_runner)
        task = _make_task()

        gen.generate(
            task,
            "diff",
            project=_make_project(),
            work_dir="/w",
        )

        prompt = docker_runner.run_claude_code.call_args.kwargs["task_prompt"]
        assert "All post-task hooks passed." in prompt

    def test_issue_number_in_prompt(self):
        docker_runner = _mock_docker_runner(VALID_YAML)
        gen = SummaryGenerator(docker_runner)
        task = _make_task()

        gen.generate(
            task,
            "diff",
            project=_make_project(),
            work_dir="/w",
        )

        prompt = docker_runner.run_claude_code.call_args.kwargs["task_prompt"]
        assert "Issue: #42" in prompt

    def test_pr_number_in_prompt(self):
        docker_runner = _mock_docker_runner(VALID_YAML)
        gen = SummaryGenerator(docker_runner)
        task = _make_task(
            source=TaskSourceInfo(type=TaskSource.GITHUB_PR, pr_number=99)
        )

        gen.generate(
            task,
            "diff",
            project=_make_project(),
            work_dir="/w",
        )

        prompt = docker_runner.run_claude_code.call_args.kwargs["task_prompt"]
        assert "PR: #99" in prompt

    def test_falls_back_to_defaults_on_parse_error(self):
        """When Claude returns unparseable output, a fallback PRMetadata is returned."""
        docker_runner = _mock_docker_runner("title: only title\n")
        gen = SummaryGenerator(docker_runner)
        task = _make_task()

        result = gen.generate(
            task,
            "diff",
            project=_make_project(),
            work_dir="/w",
        )

        assert isinstance(result, PRMetadata)
        assert result.title == task.task[:72]
        assert result.branch.startswith("wowkmang/")

    def test_falls_back_on_claude_failure(self):
        """When Claude itself fails, a fallback PRMetadata is returned."""
        runner = MagicMock()
        runner.run_claude_code.side_effect = RuntimeError("usage limit")
        gen = SummaryGenerator(runner)
        task = _make_task()

        result = gen.generate(task, "diff", project=_make_project(), work_dir="/w")

        assert isinstance(result, PRMetadata)
        assert result.branch.startswith("wowkmang/")


class TestParseResponse:
    def test_parses_json_wrapped_yaml(self):
        logs = json.dumps({"result": f"```yaml\n{VALID_YAML}\n```"})
        result = _parse_response(logs)
        assert result["title"] == "Fix login validation"
        assert result["branch"] == "fix-login-validation-42"

    def test_parses_plain_yaml(self):
        result = _parse_response(VALID_YAML)
        assert result["title"] == "Fix login validation"

    def test_malformed_raises(self):
        with pytest.raises((ValueError, Exception)):
            _parse_response("This is not yaml at all: [[[invalid")


class TestExtractYaml:
    def test_extracts_from_code_block(self):
        text = "Here is the result:\n```yaml\ntitle: test\n```\nDone."
        assert _extract_yaml(text) == "title: test"

    def test_extracts_from_plain_code_block(self):
        text = "```\ntitle: test\n```"
        assert _extract_yaml(text) == "title: test"

    def test_falls_back_to_full_text(self):
        text = "title: test\nbranch: foo"
        assert _extract_yaml(text) == "title: test\nbranch: foo"


class TestFallbackMetadata:
    def test_title_truncated_to_72_chars(self):
        task = _make_task(task="A" * 100)
        result = _fallback_metadata(task)
        assert result.title == "A" * 72

    def test_branch_has_wowkmang_prefix(self):
        task = _make_task(task="Fix the login bug")
        result = _fallback_metadata(task)
        assert result.branch.startswith("wowkmang/")

    def test_branch_is_kebab_case(self):
        task = _make_task(task="Fix the login bug")
        result = _fallback_metadata(task)
        branch = result.branch.removeprefix("wowkmang/")
        assert branch == branch.lower()
        assert " " not in branch

    def test_description_contains_task(self):
        task = _make_task(task="Fix the login bug")
        result = _fallback_metadata(task)
        assert "Fix the login bug" in result.description

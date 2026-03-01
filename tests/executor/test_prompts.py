from wowkmang.api.config import ProjectConfig
from wowkmang.executor.prompts import (
    build_task_prompt,
    issue_task_prompt,
    pr_task_prompt,
)
from wowkmang.taskqueue.models import Task, TaskSource, TaskSourceInfo


def _make_task(**overrides) -> Task:
    defaults = {
        "project": "test",
        "repo": "https://github.com/u/p",
        "task": "Fix the login bug",
        "source": TaskSourceInfo(type=TaskSource.GITHUB_ISSUE, issue_number=42),
    }
    defaults.update(overrides)
    return Task(**defaults)


def _make_project(**overrides) -> ProjectConfig:
    defaults = {"name": "test", "repo": "https://github.com/u/p"}
    defaults.update(overrides)
    return ProjectConfig(**defaults)


class TestBuildTaskPrompt:
    def test_includes_task_text(self):
        task = _make_task(task="Fix the login bug")
        prompt = build_task_prompt(task, _make_project())
        assert "Fix the login bug" in prompt

    def test_includes_unattended_warning(self):
        task = _make_task()
        prompt = build_task_prompt(task, _make_project())
        assert "unattended" in prompt
        assert "AskUserQuestion" in prompt

    def test_includes_result_file_schema(self):
        task = _make_task()
        prompt = build_task_prompt(task, _make_project())
        assert ".claude-result.json" in prompt
        assert "branch_name" in prompt

    def test_includes_issue_number(self):
        task = _make_task(
            source=TaskSourceInfo(type=TaskSource.GITHUB_ISSUE, issue_number=42)
        )
        prompt = build_task_prompt(task, _make_project())
        assert "#42" in prompt

    def test_includes_pr_number(self):
        task = _make_task(
            source=TaskSourceInfo(type=TaskSource.GITHUB_PR, pr_number=99)
        )
        prompt = build_task_prompt(task, _make_project())
        assert "#99" in prompt

    def test_questions_allowed(self):
        task = _make_task(allow_questions=True)
        prompt = build_task_prompt(task, _make_project())
        assert "ARE allowed to ask questions" in prompt

    def test_questions_not_allowed(self):
        task = _make_task(allow_questions=False)
        prompt = build_task_prompt(task, _make_project())
        assert "NOT allowed to ask questions" in prompt

    def test_includes_comments(self):
        task = _make_task()
        comments = "**@user1**:\nThis is broken\n\n---\n\n**@user2**:\n+1"
        prompt = build_task_prompt(task, _make_project(), comments=comments)
        assert "@user1" in prompt
        assert "This is broken" in prompt

    def test_no_comments_section_when_none(self):
        task = _make_task()
        prompt = build_task_prompt(task, _make_project(), comments=None)
        assert "Issue/PR Comments" not in prompt

    def test_api_source_no_source_context(self):
        task = _make_task(source=TaskSourceInfo(type=TaskSource.API))
        prompt = build_task_prompt(task, _make_project())
        assert "Source:" not in prompt


class TestIssueTaskPrompt:
    def test_includes_title_and_body(self):
        result = issue_task_prompt("Bug title", "Bug description")
        assert "Fix the issue:" in result
        assert "Bug title" in result
        assert "Bug description" in result

    def test_empty_body(self):
        result = issue_task_prompt("Title", "")
        assert "Title" in result


class TestPrTaskPrompt:
    def test_includes_title_and_body(self):
        result = pr_task_prompt("PR title", "PR body")
        assert "Address the review" in result
        assert "PR title" in result
        assert "PR body" in result

    def test_empty_body(self):
        result = pr_task_prompt("Title", "")
        assert "Title" in result

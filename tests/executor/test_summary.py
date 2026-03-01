from wowkmang.executor.summary import PRMetadata, fallback_metadata
from wowkmang.taskqueue.models import Task, TaskSource, TaskSourceInfo


def _make_task(**overrides) -> Task:
    defaults = {
        "project": "myproject",
        "repo": "https://github.com/user/project",
        "task": "Fix the login bug",
        "source": TaskSourceInfo(type=TaskSource.GITHUB_ISSUE, issue_number=42),
    }
    defaults.update(overrides)
    return Task(**defaults)


class TestFallbackMetadata:
    def test_title_truncated_to_72_chars(self):
        task = _make_task(task="A" * 100)
        result = fallback_metadata(task)
        assert result.title == "A" * 72

    def test_branch_has_wowkmang_prefix(self):
        task = _make_task(task="Fix the login bug")
        result = fallback_metadata(task)
        assert result.branch.startswith("wowkmang/")

    def test_branch_is_kebab_case(self):
        task = _make_task(task="Fix the login bug")
        result = fallback_metadata(task)
        branch = result.branch.removeprefix("wowkmang/")
        assert branch == branch.lower()
        assert " " not in branch

    def test_description_contains_task(self):
        task = _make_task(task="Fix the login bug")
        result = fallback_metadata(task)
        assert "Fix the login bug" in result.description

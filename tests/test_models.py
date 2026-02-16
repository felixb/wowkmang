from datetime import datetime, timezone

from wowkmang.models import (
    Task,
    TaskResult,
    TaskSource,
    TaskSourceInfo,
    TaskStatus,
    task_from_yaml,
    task_to_yaml,
)


class TestTaskModel:
    def test_create_task_defaults(self):
        task = Task(
            project="myproj",
            repo="https://github.com/a/b",
            task="Fix the bug",
            source=TaskSourceInfo(type=TaskSource.MANUAL),
        )
        assert len(task.id) == 8
        assert task.ref == "main"
        assert task.attempts == 0
        assert task.result is None

    def test_task_with_result(self):
        task = Task(
            project="myproj",
            repo="https://github.com/a/b",
            task="Fix the bug",
            source=TaskSourceInfo(
                type=TaskSource.GITHUB_ISSUE,
                issue_number=42,
                event="labeled",
            ),
            result=TaskResult(
                status=TaskStatus.COMPLETED,
                pr_url="https://github.com/a/b/pull/1",
                pr_number=1,
            ),
        )
        assert task.result.status == TaskStatus.COMPLETED
        assert task.source.issue_number == 42


class TestTaskSerialization:
    def test_roundtrip(self):
        task = Task(
            project="myproj",
            repo="https://github.com/a/b",
            task="Fix the bug",
            source=TaskSourceInfo(type=TaskSource.MANUAL),
        )
        yaml_str = task_to_yaml(task)
        restored = task_from_yaml(yaml_str)
        assert restored.id == task.id
        assert restored.project == task.project
        assert restored.task == task.task
        assert restored.source.type == TaskSource.MANUAL

    def test_roundtrip_with_result(self):
        task = Task(
            project="myproj",
            repo="https://github.com/a/b",
            task="Do something",
            source=TaskSourceInfo(type=TaskSource.GITHUB_ISSUE, issue_number=10),
            result=TaskResult(
                status=TaskStatus.FAILED,
                error="Timeout",
            ),
        )
        yaml_str = task_to_yaml(task)
        restored = task_from_yaml(yaml_str)
        assert restored.result.status == TaskStatus.FAILED
        assert restored.result.error == "Timeout"

    def test_yaml_contains_expected_fields(self):
        task = Task(
            project="proj",
            repo="https://github.com/x/y",
            task="hello",
            source=TaskSourceInfo(type=TaskSource.API),
        )
        yaml_str = task_to_yaml(task)
        assert "project: proj" in yaml_str
        assert "task: hello" in yaml_str
        assert "type: api" in yaml_str

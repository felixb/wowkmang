from wowkmang.models import (
    Task,
    TaskResult,
    TaskSource,
    TaskSourceInfo,
    TaskStatus,
)
from wowkmang.queue import (
    complete_task,
    ensure_queue_dirs,
    fail_task,
    get_task,
    list_tasks,
    pick_next_task,
    save_task,
)


def _make_task(**kwargs) -> Task:
    defaults = {
        "project": "testproj",
        "repo": "https://github.com/a/b",
        "task": "Do the thing",
        "source": TaskSourceInfo(type=TaskSource.MANUAL),
    }
    defaults.update(kwargs)
    return Task(**defaults)


class TestEnsureQueueDirs:
    def test_creates_dirs(self, tmp_path):
        tasks_dir = tmp_path / "tasks"
        ensure_queue_dirs(tasks_dir)
        for d in ["pending", "running", "done", "failed"]:
            assert (tasks_dir / d).is_dir()

    def test_idempotent(self, tmp_path):
        tasks_dir = tmp_path / "tasks"
        ensure_queue_dirs(tasks_dir)
        ensure_queue_dirs(tasks_dir)
        assert (tasks_dir / "pending").is_dir()


class TestSaveTask:
    def test_save_creates_file(self, tmp_tasks_dir):
        task = _make_task()
        path = save_task(tmp_tasks_dir, task)
        assert path.exists()
        assert path.parent.name == "pending"
        assert task.id in path.name


class TestPickNextTask:
    def test_pick_from_empty(self, tmp_tasks_dir):
        assert pick_next_task(tmp_tasks_dir) is None

    def test_pick_moves_to_running(self, tmp_tasks_dir):
        task = _make_task()
        save_task(tmp_tasks_dir, task)
        result = pick_next_task(tmp_tasks_dir)
        assert result is not None
        path, picked = result
        assert path.parent.name == "running"
        assert picked.id == task.id
        assert not list(tmp_tasks_dir.joinpath("pending").glob("*.yaml"))

    def test_pick_fifo_order(self, tmp_tasks_dir):
        from datetime import datetime, timezone, timedelta

        t1 = _make_task(
            id="first111",
            created=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        t2 = _make_task(
            id="second22",
            created=datetime(2025, 1, 2, tzinfo=timezone.utc),
        )
        save_task(tmp_tasks_dir, t1)
        save_task(tmp_tasks_dir, t2)
        _, picked = pick_next_task(tmp_tasks_dir)
        assert picked.id == "first111"


class TestCompleteAndFailTask:
    def test_complete_moves_to_done(self, tmp_tasks_dir):
        task = _make_task()
        save_task(tmp_tasks_dir, task)
        path, picked = pick_next_task(tmp_tasks_dir)
        picked.result = TaskResult(status=TaskStatus.COMPLETED)
        dest = complete_task(tmp_tasks_dir, path, picked)
        assert dest.parent.name == "done"
        assert not list(tmp_tasks_dir.joinpath("running").glob("*.yaml"))

    def test_fail_moves_to_failed(self, tmp_tasks_dir):
        task = _make_task()
        save_task(tmp_tasks_dir, task)
        path, picked = pick_next_task(tmp_tasks_dir)
        picked.result = TaskResult(status=TaskStatus.FAILED, error="boom")
        dest = fail_task(tmp_tasks_dir, path, picked)
        assert dest.parent.name == "failed"


class TestGetTask:
    def test_find_pending(self, tmp_tasks_dir):
        task = _make_task()
        save_task(tmp_tasks_dir, task)
        found = get_task(tmp_tasks_dir, task.id)
        assert found is not None
        assert found.id == task.id

    def test_find_in_running(self, tmp_tasks_dir):
        task = _make_task()
        save_task(tmp_tasks_dir, task)
        pick_next_task(tmp_tasks_dir)
        found = get_task(tmp_tasks_dir, task.id)
        assert found is not None

    def test_not_found(self, tmp_tasks_dir):
        assert get_task(tmp_tasks_dir, "nonexistent") is None


class TestListTasks:
    def test_list_all(self, tmp_tasks_dir):
        save_task(tmp_tasks_dir, _make_task(id="aaa11111"))
        save_task(tmp_tasks_dir, _make_task(id="bbb22222"))
        tasks = list_tasks(tmp_tasks_dir)
        assert len(tasks) == 2

    def test_list_filtered(self, tmp_tasks_dir):
        save_task(tmp_tasks_dir, _make_task(id="aaa11111"))
        save_task(tmp_tasks_dir, _make_task(id="bbb22222"))
        pick_next_task(tmp_tasks_dir)
        assert len(list_tasks(tmp_tasks_dir, status="pending")) == 1
        assert len(list_tasks(tmp_tasks_dir, status="running")) == 1
        assert len(list_tasks(tmp_tasks_dir, status="done")) == 0

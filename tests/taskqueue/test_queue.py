from wowkmang.taskqueue.models import (
    Task,
    TaskResult,
    TaskSource,
    TaskSourceInfo,
    TaskStatus,
)
from wowkmang.taskqueue.task_queue import (
    complete_task,
    ensure_queue_dirs,
    fail_task,
    get_task,
    list_tasks,
    pick_next_task,
    prune_old_tasks,
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


class TestPruneOldTasks:
    def _finish_task(self, tasks_dir, task, *, done: bool = True):
        save_task(tasks_dir, task)
        path, picked = pick_next_task(tasks_dir)
        picked.result = TaskResult(
            status=TaskStatus.COMPLETED if done else TaskStatus.FAILED,
            error=None if done else "error",
        )
        if done:
            complete_task(tasks_dir, path, picked)
        else:
            fail_task(tasks_dir, path, picked)

    def test_prune_old_done_task(self, tmp_tasks_dir):
        from datetime import datetime, timezone, timedelta

        old_task = _make_task(
            id="old11111",
            created=datetime.now(timezone.utc) - timedelta(days=8),
        )
        self._finish_task(tmp_tasks_dir, old_task, done=True)
        deleted = prune_old_tasks(tmp_tasks_dir, retention_days=7)
        assert deleted == 1
        assert not list(tmp_tasks_dir.joinpath("done").glob("*.yaml"))

    def test_prune_old_failed_task(self, tmp_tasks_dir):
        from datetime import datetime, timezone, timedelta

        old_task = _make_task(
            id="oldf1111",
            created=datetime.now(timezone.utc) - timedelta(days=10),
        )
        self._finish_task(tmp_tasks_dir, old_task, done=False)
        deleted = prune_old_tasks(tmp_tasks_dir, retention_days=7)
        assert deleted == 1
        assert not list(tmp_tasks_dir.joinpath("failed").glob("*.yaml"))

    def test_keep_recent_task(self, tmp_tasks_dir):
        from datetime import datetime, timezone, timedelta

        recent_task = _make_task(
            id="new11111",
            created=datetime.now(timezone.utc) - timedelta(days=3),
        )
        self._finish_task(tmp_tasks_dir, recent_task, done=True)
        deleted = prune_old_tasks(tmp_tasks_dir, retention_days=7)
        assert deleted == 0
        assert list(tmp_tasks_dir.joinpath("done").glob("*.yaml"))

    def test_prune_mixed(self, tmp_tasks_dir):
        from datetime import datetime, timezone, timedelta

        old_task = _make_task(
            id="old11111",
            created=datetime.now(timezone.utc) - timedelta(days=8),
        )
        recent_task = _make_task(
            id="new11111",
            created=datetime.now(timezone.utc) - timedelta(days=2),
        )
        self._finish_task(tmp_tasks_dir, old_task, done=True)
        self._finish_task(tmp_tasks_dir, recent_task, done=True)
        deleted = prune_old_tasks(tmp_tasks_dir, retention_days=7)
        assert deleted == 1
        remaining = list(tmp_tasks_dir.joinpath("done").glob("*.yaml"))
        assert len(remaining) == 1
        assert "new11111" in remaining[0].name

    def test_does_not_prune_pending_or_running(self, tmp_tasks_dir):
        from datetime import datetime, timezone, timedelta

        old_pending = _make_task(
            id="pend1111",
            created=datetime.now(timezone.utc) - timedelta(days=30),
        )
        save_task(tmp_tasks_dir, old_pending)
        deleted = prune_old_tasks(tmp_tasks_dir, retention_days=7)
        assert deleted == 0
        assert list(tmp_tasks_dir.joinpath("pending").glob("*.yaml"))

    def test_returns_zero_when_empty(self, tmp_tasks_dir):
        assert prune_old_tasks(tmp_tasks_dir, retention_days=7) == 0

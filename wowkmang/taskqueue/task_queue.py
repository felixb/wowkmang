from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from wowkmang.taskqueue.models import Task, TaskStatus, task_from_yaml, task_to_yaml


class QueueDir(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    WAITING = "waiting"


STATUS_TO_DIR = {
    TaskStatus.PENDING: QueueDir.PENDING,
    TaskStatus.RUNNING: QueueDir.RUNNING,
    TaskStatus.COMPLETED: QueueDir.DONE,
    TaskStatus.FAILED: QueueDir.FAILED,
    TaskStatus.TIMEOUT: QueueDir.FAILED,
    TaskStatus.WAITING_FOR_INPUT: QueueDir.WAITING,
}


def ensure_queue_dirs(tasks_dir: Path) -> None:
    for d in QueueDir:
        (tasks_dir / d).mkdir(parents=True, exist_ok=True)


def _task_filename(task: Task) -> str:
    timestamp = task.created.strftime("%Y-%m-%dT%H-%M-%S")
    return f"{timestamp}_{task.id}.yaml"


def save_task(tasks_dir: Path, task: Task) -> Path:
    path = tasks_dir / QueueDir.PENDING / _task_filename(task)
    tmp_path = tasks_dir / QueueDir.PENDING / f".tmp_{task.id}.yaml"
    tmp_path.write_text(task_to_yaml(task))
    tmp_path.rename(path)
    return path


def pick_next_task(tasks_dir: Path) -> tuple[Path, Task] | None:
    pending_dir = tasks_dir / QueueDir.PENDING
    files = sorted(pending_dir.glob("*.yaml"))
    if not files:
        return None
    task_file = files[0]
    dest = tasks_dir / QueueDir.RUNNING / task_file.name
    try:
        task_file.rename(dest)
    except FileNotFoundError:
        return None
    task = task_from_yaml(dest.read_text())
    return dest, task


def _finish_task(
    tasks_dir: Path, task_file: Path, task: Task, dest_dir: QueueDir
) -> Path:
    dest = tasks_dir / dest_dir / task_file.name
    task_file.write_text(task_to_yaml(task))
    task_file.rename(dest)
    return dest


def complete_task(tasks_dir: Path, task_file: Path, task: Task) -> Path:
    return _finish_task(tasks_dir, task_file, task, QueueDir.DONE)


def fail_task(tasks_dir: Path, task_file: Path, task: Task) -> Path:
    return _finish_task(tasks_dir, task_file, task, QueueDir.FAILED)


def wait_for_input(tasks_dir: Path, task_file: Path, task: Task) -> Path:
    return _finish_task(tasks_dir, task_file, task, QueueDir.WAITING)


def resume_task(tasks_dir: Path, task_id: str) -> bool:
    """Move a waiting task back to pending. Returns True if found and moved."""
    waiting_dir = tasks_dir / QueueDir.WAITING
    for path in waiting_dir.glob("*.yaml"):
        if task_id in path.name:
            dest = tasks_dir / QueueDir.PENDING / path.name
            path.rename(dest)
            return True
    return False


def find_waiting_task_by_source(
    tasks_dir: Path, issue_number: int | None, pr_number: int | None
) -> Task | None:
    """Find a waiting task that matches the given source issue or PR number."""
    waiting_dir = tasks_dir / QueueDir.WAITING
    if not waiting_dir.exists():
        return None
    for path in waiting_dir.glob("*.yaml"):
        task = task_from_yaml(path.read_text())
        if issue_number and task.source.issue_number == issue_number:
            return task
        if pr_number and task.source.pr_number == pr_number:
            return task
    return None


def get_task(tasks_dir: Path, task_id: str) -> Task | None:
    for d in QueueDir:
        for path in (tasks_dir / d).glob("*.yaml"):
            if task_id in path.name:
                return task_from_yaml(path.read_text())
    return None


def list_tasks(tasks_dir: Path, status: str | None = None) -> list[Task]:
    if status:
        try:
            dirs = [QueueDir(status)]
        except ValueError:
            dirs = list(QueueDir)
    else:
        dirs = list(QueueDir)
    tasks: list[Task] = []
    for d in dirs:
        dir_path = tasks_dir / d
        if not dir_path.exists():
            continue
        for path in sorted(dir_path.glob("*.yaml")):
            tasks.append(task_from_yaml(path.read_text()))
    return tasks


def prune_old_tasks(tasks_dir: Path, retention_days: int) -> int:
    """Delete finished tasks (done/failed) older than retention_days. Returns count deleted."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    deleted = 0
    for d in (QueueDir.DONE, QueueDir.FAILED):
        dir_path = tasks_dir / d
        if not dir_path.exists():
            continue
        for path in dir_path.glob("*.yaml"):
            # Filename format: 2025-02-16T14-32-00_<id>.yaml
            # Parse the timestamp prefix to avoid reading each YAML file
            stem = path.stem  # e.g. "2025-02-16T14-32-00_a1b2c3d4"
            ts_part = stem.split("_")[0]  # "2025-02-16T14-32-00"
            try:
                created = datetime.strptime(ts_part, "%Y-%m-%dT%H-%M-%S").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
            if created < cutoff:
                path.unlink()
                deleted += 1
    return deleted

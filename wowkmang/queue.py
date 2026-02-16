from enum import Enum
from pathlib import Path

from wowkmang.models import Task, TaskStatus, task_from_yaml, task_to_yaml


class QueueDir(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


STATUS_TO_DIR = {
    TaskStatus.PENDING: QueueDir.PENDING,
    TaskStatus.RUNNING: QueueDir.RUNNING,
    TaskStatus.COMPLETED: QueueDir.DONE,
    TaskStatus.FAILED: QueueDir.FAILED,
    TaskStatus.TIMEOUT: QueueDir.FAILED,
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

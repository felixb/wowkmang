import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class TaskSource(str, Enum):
    GITHUB_ISSUE = "github_issue"
    GITHUB_PR = "github_pr"
    MANUAL = "manual"
    API = "api"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


class TaskSourceInfo(BaseModel):
    type: TaskSource
    issue_number: Optional[int] = None
    pr_number: Optional[int] = None
    event: Optional[str] = None


class TaskResult(BaseModel):
    status: TaskStatus
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    branch: Optional[str] = None
    duration_seconds: Optional[int] = None
    finished: Optional[datetime] = None
    post_task_passed: Optional[bool] = None
    fix_attempts: Optional[int] = None
    error: Optional[str] = None


class Task(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    project: str
    repo: str
    ref: str = "main"
    task: str
    model: Optional[str] = None
    source: TaskSourceInfo
    attempts: int = 0
    max_attempts: int = 3
    result: Optional[TaskResult] = None


def task_to_yaml(task: Task) -> str:
    return yaml.dump(
        task.model_dump(mode="json", exclude_none=True),
        default_flow_style=False,
        sort_keys=False,
    )


def task_from_yaml(raw: str) -> Task:
    data = yaml.safe_load(raw)
    return Task(**data)

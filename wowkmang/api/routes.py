import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

import docker
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from wowkmang.api.auth import Authenticator, verify_github_signature
from wowkmang.api.config import (
    GlobalConfig,
    find_project_by_repo,
    load_projects,
    ProjectConfig,
)
from wowkmang.executor.docker_runner import DockerRunner
from wowkmang.executor.hooks import HookRunner
from wowkmang.taskqueue.models import Task, TaskSource, TaskSourceInfo
from wowkmang.taskqueue.task_queue import (
    ensure_queue_dirs,
    get_task,
    list_tasks,
    save_task,
)
from wowkmang.executor.repo_cache import RepoCache
from wowkmang.executor.summary import SummaryGenerator
from wowkmang.executor.worker import FixLoop, Worker

logger = logging.getLogger(__name__)

config = GlobalConfig()
projects: dict[str, ProjectConfig] = {}
authenticator: Authenticator | None = None
worker: Worker | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global projects, authenticator, worker
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)
    log_format = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    formatter = logging.Formatter(log_format)
    for name in (None, "uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.setLevel(log_level)
        for handler in lg.handlers:
            handler.setFormatter(formatter)
    if not logging.getLogger().handlers:
        logging.basicConfig(format=log_format, level=log_level)
    ensure_queue_dirs(config.tasks_dir)
    projects = load_projects(config.projects_dir)
    authenticator = Authenticator(config, projects)

    # Initialize worker components
    docker_client = docker.from_env()
    docker_runner = DockerRunner(
        docker_client,
        pull_token=config.pull_token,
        github_token=config.github_token,
        default_uid=config.container_uid,
        default_docker_image=config.docker_image,
    )
    repo_cache = RepoCache(docker_runner=docker_runner)
    hook_runner = HookRunner(docker_runner)
    fix_loop = FixLoop(docker_runner, hook_runner)
    summary_generator = SummaryGenerator(docker_runner)

    worker = Worker(
        config=config,
        projects=projects,
        docker_runner=docker_runner,
        repo_cache=repo_cache,
        hook_runner=hook_runner,
        fix_loop=fix_loop,
        summary_generator=summary_generator,
    )
    worker.start()
    logger.info("Worker started")

    yield

    worker.stop()
    logger.info("Worker stopped")


app = FastAPI(title="wowkmang", lifespan=lifespan)


async def authenticate(request: Request) -> dict:
    if authenticator is None:
        raise HTTPException(status_code=500, detail="Not initialized")
    return await authenticator(request)


async def try_authenticate(request: Request) -> dict | None:
    if authenticator is None:
        return None
    try:
        return await authenticator(request)
    except HTTPException:
        return None


class CreateTaskRequest(BaseModel):
    project: str
    task: str
    ref: Optional[str] = None
    model: Optional[str] = None


@app.get("/health")
async def health(auth: dict | None = Depends(try_authenticate)):
    if not auth:
        return {"status": "ok"}
    pending = list_tasks(config.tasks_dir, status="pending")
    return {
        "status": "ok",
        "worker": worker.status if worker else "not_started",
        "queue_depth": len(pending),
    }


@app.post("/tasks", status_code=202)
async def create_task(body: CreateTaskRequest, auth: dict = Depends(authenticate)):
    project = projects.get(body.project)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    task = Task(
        project=body.project,
        repo=project.repo,
        ref=body.ref or project.ref,
        task=body.task,
        model=body.model,
        source=TaskSourceInfo(type=TaskSource.API),
    )
    save_task(config.tasks_dir, task)
    return {"id": task.id, "status": "pending", "project": task.project}


@app.get("/tasks")
async def get_tasks(status: Optional[str] = None, auth: dict = Depends(authenticate)):
    tasks = list_tasks(config.tasks_dir, status=status)
    return [t.model_dump(mode="json", exclude_none=True) for t in tasks]


@app.get("/tasks/{task_id}")
async def get_task_endpoint(task_id: str, auth: dict = Depends(authenticate)):
    task = get_task(config.tasks_dir, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task.model_dump(mode="json", exclude_none=True)


@app.post("/webhooks/github", status_code=202)
async def github_webhook(request: Request):
    sig_header = request.headers.get("x-hub-signature-256")
    if not sig_header:
        raise HTTPException(status_code=401, detail="Missing signature")

    body = await request.body()
    event_type = request.headers.get("x-github-event", "")
    payload = json.loads(body)

    repo_full_name = payload.get("repository", {}).get("full_name", "")
    project = find_project_by_repo(repo_full_name, projects)
    if not project:
        return {"status": "ignored", "reason": "Unknown repository"}

    if not verify_github_signature(body, sig_header, project.webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid signature")

    action = payload.get("action")
    label_name = payload.get("label", {}).get("name", "")

    if label_name != project.github_labels.trigger:
        return {"status": "ignored", "reason": "Irrelevant label"}

    if event_type == "issues" and action == "labeled":
        issue = payload["issue"]
        task = Task(
            project=project.name,
            repo=f"https://github.com/{repo_full_name}",
            ref=project.ref,
            task=f"Fix the issue:\n\nTitle: {issue['title']}\n\n{issue.get('body') or ''}",
            source=TaskSourceInfo(
                type=TaskSource.GITHUB_ISSUE,
                issue_number=issue["number"],
                event="labeled",
            ),
        )
        save_task(config.tasks_dir, task)
        return {"status": "accepted", "task_id": task.id}

    if event_type == "pull_request" and action == "labeled":
        pr = payload["pull_request"]
        task = Task(
            project=project.name,
            repo=f"https://github.com/{repo_full_name}",
            ref=project.ref,
            task=f"Address the review on this PR:\n\nTitle: {pr['title']}\n\n{pr.get('body') or ''}",
            source=TaskSourceInfo(
                type=TaskSource.GITHUB_PR,
                pr_number=pr["number"],
                event="labeled",
            ),
        )
        save_task(config.tasks_dir, task)
        return {"status": "accepted", "task_id": task.id}

    return {
        "status": "ignored",
        "reason": f"Unhandled event: {event_type}/{action}",
    }

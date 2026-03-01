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
from wowkmang.executor.github_client import fetch_and_save_comments
from wowkmang.executor.hooks import HookRunner
from wowkmang.executor.prompts import issue_task_prompt, pr_task_prompt
from wowkmang.taskqueue.models import (
    Task,
    TaskSource,
    TaskSourceInfo,
    task_to_yaml,
)
from wowkmang.taskqueue.task_queue import (
    ensure_queue_dirs,
    find_waiting_task_by_source,
    get_task,
    list_tasks,
    resume_task,
    save_task,
)
from wowkmang.executor.repo_cache import RepoCache
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

    worker = Worker(
        config=config,
        projects=projects,
        docker_runner=docker_runner,
        repo_cache=repo_cache,
        hook_runner=hook_runner,
        fix_loop=fix_loop,
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
    allow_questions: bool = False


class AnswerRequest(BaseModel):
    answers: list[str]


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
        allow_questions=body.allow_questions,
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


@app.get("/tasks/{task_id}/questions")
async def get_task_questions(task_id: str, auth: dict = Depends(authenticate)):
    task = get_task(config.tasks_dir, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.result or not task.result.questions:
        return {"questions": []}
    return {"questions": task.result.questions}


@app.post("/tasks/{task_id}/answers", status_code=202)
async def post_task_answers(
    task_id: str, body: AnswerRequest, auth: dict = Depends(authenticate)
):
    task = get_task(config.tasks_dir, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.result or task.result.status != "waiting_for_input":
        raise HTTPException(status_code=400, detail="Task is not waiting for input")

    # Append answers to the task prompt
    answers_text = "\n\nAnswers to previous questions:\n"
    for i, answer in enumerate(body.answers):
        answers_text += f"- {answer}\n"
    task.task += answers_text
    task.result = None

    # Update the task file in the waiting dir, then move to pending
    waiting_dir = config.tasks_dir / "waiting"
    for path in waiting_dir.glob(f"*_{task_id}.yaml"):
        path.write_text(task_to_yaml(task))

    if not resume_task(config.tasks_dir, task_id):
        raise HTTPException(status_code=400, detail="Could not resume task")

    return {"status": "resumed", "task_id": task_id}


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

    github_token = project.github_token or config.github_token

    # Handle comments as answers to waiting tasks
    if event_type == "issue_comment" and action == "created":
        comment_body = payload.get("comment", {}).get("body", "")
        issue_data = payload.get("issue", {})
        issue_number = issue_data.get("number")
        is_pr = "pull_request" in issue_data

        waiting_task = find_waiting_task_by_source(
            config.tasks_dir,
            issue_number=None if is_pr else issue_number,
            pr_number=issue_number if is_pr else None,
        )
        if not waiting_task:
            return {"status": "ignored", "reason": "No waiting task for this issue/PR"}

        waiting_task.task += f"\n\nAnswer from GitHub comment:\n- {comment_body}\n"
        waiting_task.result = None

        waiting_dir = config.tasks_dir / "waiting"
        for path in waiting_dir.glob(f"*_{waiting_task.id}.yaml"):
            path.write_text(task_to_yaml(waiting_task))

        if not resume_task(config.tasks_dir, waiting_task.id):
            return {"status": "error", "reason": "Could not resume task"}

        return {"status": "resumed", "task_id": waiting_task.id}

    if label_name != project.github_labels.trigger:
        return {"status": "ignored", "reason": "Irrelevant label"}

    if event_type == "issues" and action == "labeled":
        issue = payload["issue"]
        task = Task(
            project=project.name,
            repo=f"https://github.com/{repo_full_name}",
            ref=project.ref,
            task=issue_task_prompt(issue["title"], issue.get("body") or ""),
            source=TaskSourceInfo(
                type=TaskSource.GITHUB_ISSUE,
                issue_number=issue["number"],
                event="labeled",
            ),
        )

        # Fetch and save comments
        task.comments_file = fetch_and_save_comments(
            github_token=github_token,
            repo_full_name=repo_full_name,
            source_type=TaskSource.GITHUB_ISSUE,
            source_number=issue["number"],
            task_id=task.id,
            context_dir=config.tasks_dir / "context",
        )

        save_task(config.tasks_dir, task)
        return {"status": "accepted", "task_id": task.id}

    if event_type == "pull_request" and action == "labeled":
        pr = payload["pull_request"]

        # Get PR head branch
        pr_branch = pr.get("head", {}).get("ref")

        task = Task(
            project=project.name,
            repo=f"https://github.com/{repo_full_name}",
            ref=project.ref,
            task=pr_task_prompt(pr["title"], pr.get("body") or ""),
            source=TaskSourceInfo(
                type=TaskSource.GITHUB_PR,
                pr_number=pr["number"],
                event="labeled",
            ),
            pr_branch=pr_branch,
        )

        # Fetch and save comments
        task.comments_file = fetch_and_save_comments(
            github_token=github_token,
            repo_full_name=repo_full_name,
            source_type=TaskSource.GITHUB_PR,
            source_number=pr["number"],
            task_id=task.id,
            context_dir=config.tasks_dir / "context",
        )

        save_task(config.tasks_dir, task)
        return {"status": "accepted", "task_id": task.id}

    return {
        "status": "ignored",
        "reason": f"Unhandled event: {event_type}/{action}",
    }

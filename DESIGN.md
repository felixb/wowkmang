# wowkmang — Design Document

> Autonomous task runner for Claude Code. Receives tasks via webhooks, runs Claude Code in isolated Docker containers, and produces pull requests.
>
> Named after the Belter Creole word for "worker" — the one who does the wowk that the bosmang orders.

## Overview

wowkmang is a standalone, self-hosted service that:

1. Receives tasks via webhooks (GitHub events, manual API calls, or future integrations like bosmang)
1. Queues them as YAML files in a filesystem-based queue
1. Picks them up one at a time with a background worker thread
1. Spins up isolated Docker containers running Claude Code against the target repo
1. Runs pre/post-task hooks (dependency install, tests, linting)
1. Opens pull requests with the results

It is designed to be project-agnostic. Pointing it at a new repo is just adding a project config file and a webhook.

## Architecture

```
Host
├── Docker daemon
├── wowkmang container (mounts docker.sock)
│     ├── FastAPI (webhook receiver + manual API)
│     └── Worker thread (picks up tasks, manages container lifecycle)
├── Claude Code container (current task) ← sibling, not child
└── Shared volumes
      ├── tasks/       (pending/running/done/failed)
      ├── cache/repos/ (bare clones for fast checkout)
      ├── cache/uv/    (UV_CACHE_DIR)
      ├── cache/pip/   (PIP_CACHE_DIR)
      ├── cache/npm/   (npm/yarn/pnpm cache)
      └── cache/tf/    (TF_PLUGIN_CACHE_DIR)
```

### Docker: Socket Mount, Not DinD

wowkmang does NOT use Docker-in-Docker. Instead, the host's Docker socket is mounted into the wowkmang container:

```
docker run -v /var/run/docker.sock:/var/run/docker.sock ...
```

Claude Code containers run as **siblings** on the host, not nested inside wowkmang. This is the standard pattern used by CI systems like Jenkins and GitLab Runner.

**Important volume mount caveat**: When wowkmang tells Docker to mount a volume into a Claude Code container, the path must be valid on the **host**, not inside the wowkmang container. Use a configurable host path prefix or named Docker volumes for shared data. The host path prefix is passed to wowkmang via an environment variable (e.g. `WOWKMANG_HOST_DATA_DIR`).

Running the web service outside Docker would grant the same Docker socket permissions, so containerizing wowkmang does not increase the attack surface — it adds a thin layer of isolation for the API process itself.

## Project Structure

```
wowkmang/
  wowkmang/
    __init__.py
    api.py                # FastAPI app, webhook endpoints, manual task creation
    auth.py               # Token auth + GitHub webhook signature verification
    worker.py             # Background worker thread, task pickup loop
    docker_runner.py      # Container lifecycle: create, run, collect results
    repo_cache.py         # Clone/fetch/copy logic for repo caching
    github_client.py      # PR creation, branch management, labels
    config.py             # Global + project config models (Pydantic)
    models.py             # Task schema (Pydantic, serializes to/from YAML)
    summary.py            # Haiku-based PR metadata generation
    hooks.py              # Pre/post-task hook execution and fix loop
  projects/               # Per-project config YAML files
    example.yaml
  tasks/                  # Filesystem-based task queue
    pending/
    running/
    done/
    failed/
  cache/
    repos/
    uv/
    pip/
    npm/
    tf/
  Dockerfile
  docker-compose.yaml
  pyproject.toml
  README.md
```

## Configuration

### Global Configuration

Global settings are minimal. The Claude API key is read from the existing Claude Code installation on the host (mounted or extracted into the container). No separate key management.

Environment variables for wowkmang itself:

```bash
# Required
WOWKMANG_HOST_DATA_DIR=/opt/wowkmang       # Host path prefix for Docker volume mounts
WOWKMANG_PROJECTS_DIR=./projects            # Path to project config directory
WOWKMANG_TASKS_DIR=./tasks                  # Path to task queue directory
WOWKMANG_CACHE_DIR=./cache                  # Path to cache directory

# API server
WOWKMANG_HOST=0.0.0.0
WOWKMANG_PORT=8484

# Auth
WOWKMANG_API_TOKENS=token1,token2           # Comma-separated API bearer tokens (or references)
```

### Project Configuration

Each project is defined by a YAML file in the `projects/` directory.

```yaml
# projects/myproject.yaml

# Project identity
name: myproject
repo: https://github.com/user/project
ref: main                                     # Default branch to work against

# Credentials — scoped to this project only
# These end up in the Claude Code container's environment.
# Only provide what this project needs. Least privilege by design.
credentials:
  github_token: ghp_xxxxxxxxxxxxxxxxxxxx
  # Add any project-specific env vars Claude Code might need
  # e.g. DATABASE_URL, AWS keys for tests, etc.

# Claude Code settings
default_model: claude-sonnet-4-5-20250929     # Model for the main task
extra_instructions: |                          # Appended to system prompt alongside repo's CLAUDE.md
  Always write tests for new functionality.
  Follow the existing code style.
  Use conventional commit messages.

# Docker settings for Claude Code container
docker_image: ghcr.io/anthropics/claude-code:latest  # Or a custom image
timeout_minutes: 30                            # Max runtime per task
max_fix_attempts: 2                            # Max retries for fix_or_* policies

# Hooks — run inside the Claude Code container
pre_task:
  - uv sync
  - uv run pre-commit install

post_task:
  - uv run pre-commit run --all-files
  - uv run pytest

post_task_policy: fix_or_warn                  # One of: fail, warn, fix_or_fail, fix_or_warn

# GitHub integration
github_labels:
  trigger: wowkmang                            # Label that triggers task creation
  in_progress: wowkmang/in-progress            # Applied while task is running
  done: wowkmang/done                          # Applied when PR is opened
  failed: wowkmang/failed                      # Applied on failure
  needs_attention: wowkmang/needs-attention     # Applied to draft PRs (checks didn't pass)

# Webhook
webhook_secret: whsec_xxxxxxxxxxxx             # GitHub webhook secret for HMAC verification
```

### File Permissions

The `projects/` directory contains secrets and must be readable only by the wowkmang process:

```bash
chmod 700 projects/
chmod 600 projects/*.yaml
```

## Authentication

Two authentication mechanisms, checked per-request based on which headers are present:

### 1. Bearer Token Auth (Manual API calls, bosmang integration)

```
Authorization: Bearer <token>
```

- Tokens are configured via `WOWKMANG_API_TOKENS` environment variable
- Stored and compared as hashes (SHA-256)
- Use `hmac.compare_digest()` for constant-time comparison
- Each token should be scoped: the API checks that the token is authorized for the requested project
- Rate limiting per token to prevent runaway costs

### 2. GitHub Webhook Signature (GitHub events)

```
X-Hub-Signature-256: sha256=<hex_digest>
```

- Each project config has a `webhook_secret`
- Verify HMAC-SHA256 of the raw request body against the project's secret
- Use `hmac.compare_digest()` for constant-time comparison
- The project is identified from the webhook payload (repository full name)

### Auth Middleware Flow

```python
async def authenticate(request: Request) -> AuthContext:
    if "authorization" in request.headers:
        # Bearer token auth
        token = extract_bearer_token(request.headers["authorization"])
        return verify_api_token(token)
    elif "x-hub-signature-256" in request.headers:
        # GitHub webhook signature
        body = await request.body()
        repo = extract_repo_from_payload(body)
        project = find_project_by_repo(repo)
        verify_github_signature(body, request.headers["x-hub-signature-256"], project.webhook_secret)
        return AuthContext(project=project, source="github")
    else:
        raise HTTPException(401, "No authentication provided")
```

## Task Queue

### Filesystem-Based Queue

Tasks are stored as individual YAML files, organized into directories by state:

```
tasks/
  pending/      # Waiting to be picked up
  running/      # Currently being processed
  done/         # Completed successfully
  failed/       # Failed after all attempts
```

### State Transitions

State transitions are **file renames** (`os.rename()`), which are atomic on the same filesystem:

```
pending/ → running/ → done/
                    → failed/
```

### File Naming

```
{iso_timestamp}_{random_id}.yaml
```

Example: `2025-02-16T14-32-00_a1b2c3d4.yaml`

The timestamp prefix ensures FIFO ordering when the worker lists and sorts `pending/`.

### Task Schema

```yaml
# Task definition — written at creation time
id: a1b2c3d4
created: "2025-02-16T14:32:00Z"
project: myproject                              # References projects/myproject.yaml
repo: https://github.com/user/project
ref: main
task: "Fix the login bug described in issue #42"
model: claude-opus-4-5-20250929                 # Optional override, falls back to project default
source:
  type: github_issue                            # or: github_pr, manual, api
  issue_number: 42                              # If triggered by GitHub
  pr_number: null
  event: labeled                                # GitHub event type if applicable
attempts: 0
max_attempts: 3

# Result — appended by worker after completion
result:
  status: completed                             # or: failed, timeout
  pr_url: https://github.com/user/project/pull/87
  pr_number: 87
  branch: wowkmang/fix-login-bug-42
  duration_seconds: 342
  finished: "2025-02-16T14:37:42Z"
  post_task_passed: false                       # Whether post-task hooks passed
  fix_attempts: 2                               # How many fix loops were attempted
  error: null                                   # Error message if failed
```

### Pydantic Model

```python
from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid

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
    created: datetime = Field(default_factory=datetime.utcnow)
    project: str
    repo: str
    ref: str = "main"
    task: str                                    # The actual prompt/description
    model: Optional[str] = None                  # Override project default
    source: TaskSourceInfo
    attempts: int = 0
    max_attempts: int = 3
    result: Optional[TaskResult] = None
```

## API Endpoints

### POST /webhooks/github

Receives GitHub webhook events. Authenticated via `X-Hub-Signature-256`.

**Trigger conditions:**

- Issue labeled with the project's `github_labels.trigger` label (e.g. `wowkmang`)
- PR labeled with the project's `github_labels.trigger` label

**Task creation from GitHub events:**

- For labeled issues: task prompt is the issue title + body
- For labeled PRs: task prompt is the PR title + body + "Address the review comments on this PR" (if there are reviews)

**Response:** `202 Accepted` with task ID, or `200 OK` if event is irrelevant.

### POST /tasks

Manual task creation. Authenticated via Bearer token.

**Request body:**

```json
{
  "project": "myproject",
  "task": "Fix the login bug described in issue #42",
  "ref": "main",
  "model": "claude-opus-4-5-20250929"
}
```

Only `project` and `task` are required. `ref` and `model` fall back to project defaults.

**Response:** `202 Accepted` with task ID.

```json
{
  "id": "a1b2c3d4",
  "status": "pending",
  "project": "myproject"
}
```

### GET /tasks/{task_id}

Returns current task status. Authenticated via Bearer token.

### GET /health

Unauthenticated health check. Returns worker status and queue depth.

```json
{
  "status": "ok",
  "worker": "idle",
  "queue_depth": 3
}
```

## Worker

### Single-Threaded Background Worker

The worker runs as a background thread in the same FastAPI process. It loops, checking for pending tasks:

```python
import threading
import time

class Worker:
    def __init__(self, tasks_dir: Path, projects_dir: Path, ...):
        self.tasks_dir = tasks_dir
        self.running = False
        self._thread = None

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self):
        while self.running:
            task_file = self._pick_next_task()
            if task_file:
                self._process_task(task_file)
            else:
                time.sleep(5)  # Poll interval

    def _pick_next_task(self) -> Optional[Path]:
        """Pick oldest pending task (sorted by filename timestamp)."""
        pending = sorted(self.tasks_dir.joinpath("pending").glob("*.yaml"))
        if not pending:
            return None
        task_file = pending[0]
        # Atomic move to running/
        dest = self.tasks_dir / "running" / task_file.name
        try:
            task_file.rename(dest)
            return dest
        except FileNotFoundError:
            return None  # Another process grabbed it (future-proofing)
```

### Task Processing Flow

```
_process_task(task_file):
  1. Load task YAML
  2. Load project config
  3. Prepare working directory:
     a. Clone repo (using cache) into temp dir
     b. Checkout correct ref, create working branch
  4. Run pre_task hooks in container
     → If any fail: mark task as failed, move to failed/, stop
  5. Run Claude Code in container with task prompt
  6. Run post_task hooks in container
     → If pass: proceed to PR creation
     → If fail: apply post_task_policy (see below)
  7. Generate PR metadata via Haiku summary call
  8. Push branch, open PR (draft if checks didn't pass)
  9. Update task YAML with result
  10. Move task file to done/ (or failed/)
```

### Crash Recovery

If wowkmang restarts (e.g. systemd restart), any task files in `running/` are stale — the container they were running in is gone. On startup, the worker should:

1. List all files in `running/`
1. Check if the corresponding Docker container is still alive
1. If not, either move back to `pending/` for retry (if attempts < max_attempts) or move to `failed/`

## Docker Runner

### Container Lifecycle

```python
import docker

class DockerRunner:
    def __init__(self, docker_client: docker.DockerClient, host_data_dir: str):
        self.client = docker_client
        self.host_data_dir = host_data_dir

    def run_claude_code(
        self,
        work_dir: str,          # Host path to the cloned repo
        task_prompt: str,
        model: str,
        project: ProjectConfig,
        timeout_minutes: int,
    ) -> ContainerResult:
        """Spin up a Claude Code container and run a task."""

        environment = {
            "CLAUDE_MODEL": model,
            **project.credentials,  # Project-scoped secrets
        }

        volumes = {
            work_dir: {"bind": "/workspace", "mode": "rw"},
            # Dependency caches (host paths)
            f"{self.host_data_dir}/cache/uv": {"bind": "/cache/uv", "mode": "rw"},
            f"{self.host_data_dir}/cache/pip": {"bind": "/cache/pip", "mode": "rw"},
            f"{self.host_data_dir}/cache/npm": {"bind": "/cache/npm", "mode": "rw"},
            f"{self.host_data_dir}/cache/tf": {"bind": "/cache/tf", "mode": "rw"},
        }

        container = self.client.containers.run(
            image=project.docker_image,
            command=self._build_command(task_prompt, project),
            environment=environment,
            volumes=volumes,
            working_dir="/workspace",
            detach=True,
            # Resource limits
            mem_limit="4g",
            cpu_period=100000,
            cpu_quota=200000,  # 2 CPUs
        )

        try:
            result = container.wait(timeout=timeout_minutes * 60)
            logs = container.logs().decode()
            return ContainerResult(
                exit_code=result["StatusCode"],
                logs=logs,
            )
        except Exception as e:
            container.kill()
            raise
        finally:
            container.remove()

    def run_hooks(
        self,
        work_dir: str,
        commands: list[str],
        project: ProjectConfig,
    ) -> HookResult:
        """Run pre/post-task hooks in a container."""
        # Similar to run_claude_code but runs shell commands
        # instead of Claude Code
        script = " && ".join(commands)
        # ... (same container setup, different command)
```

### Claude Code Invocation

The exact command to invoke Claude Code inside the container depends on the Claude Code Docker image. The task prompt and any extra instructions from the project config are passed as arguments or via a prompt file mounted into the container.

```python
def _build_command(self, task_prompt: str, project: ProjectConfig) -> str:
    # Build the full prompt including project extra_instructions
    # The repo's CLAUDE.md is already in /workspace and Claude Code picks it up automatically
    full_prompt = task_prompt
    if project.extra_instructions:
        full_prompt = f"{project.extra_instructions}\n\n{task_prompt}"

    # Claude Code headless invocation
    return f'claude --model {project.default_model} --print "{full_prompt}"'
```

## Repo Caching

### Strategy: Cached Bare Clone + git clone --reference

```
cache/
  repos/
    github.com_user_project/    ← bare mirror
```

**On task start:**

```python
class RepoCache:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir

    def prepare_workdir(self, repo_url: str, ref: str, work_dir: Path) -> Path:
        """Clone repo into work_dir using cache for speed."""
        cache_path = self._cache_path(repo_url)

        # Update or create cache
        if cache_path.exists():
            subprocess.run(["git", "fetch", "--all"], cwd=cache_path, check=True)
        else:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--bare", repo_url, str(cache_path)],
                check=True,
            )

        # Clone using cache as reference (fast, uses local objects)
        subprocess.run(
            ["git", "clone", "--reference", str(cache_path), repo_url, str(work_dir)],
            check=True,
        )

        # Create working branch
        branch_name = f"wowkmang/{uuid.uuid4().hex[:8]}"
        subprocess.run(["git", "checkout", "-b", branch_name, f"origin/{ref}"], cwd=work_dir, check=True)

        return branch_name

    def _cache_path(self, repo_url: str) -> Path:
        """Convert repo URL to cache directory name."""
        # https://github.com/user/project → github.com_user_project
        sanitized = repo_url.replace("https://", "").replace("/", "_").rstrip(".git")
        return self.cache_dir / sanitized
```

The `--reference` flag makes git use the cached objects locally but creates a fully independent clone. Safe for concurrent use (future-proofing).

## Pre/Post-Task Hooks

### Execution

Hooks run inside the same Docker container (or a matching one) as Claude Code, in the same working directory. This ensures the environment matches exactly.

```python
class HookRunner:
    def run_pre_hooks(self, commands: list[str], work_dir: str, project: ProjectConfig) -> HookResult:
        """Run pre-task hooks. Failure = task fails immediately (no API credits burned)."""
        return self._run_hooks(commands, work_dir, project)

    def run_post_hooks(self, commands: list[str], work_dir: str, project: ProjectConfig) -> HookResult:
        """Run post-task hooks. Captures output for fix loops and PR body."""
        return self._run_hooks(commands, work_dir, project)

    def _run_hooks(self, commands: list[str], work_dir: str, project: ProjectConfig) -> HookResult:
        result = docker_runner.run_hooks(work_dir, commands, project)
        return HookResult(
            success=result.exit_code == 0,
            output=result.logs,
            exit_code=result.exit_code,
        )
```

### Post-Task Policies

| Policy        | Checks Pass | Checks Fail (fix succeeds) | Checks Fail (fix fails)        |
| ------------- | ----------- | -------------------------- | ------------------------------ |
| `fail`        | Regular PR  | N/A (no fix attempt)       | No PR, task fails              |
| `warn`        | Regular PR  | N/A (no fix attempt)       | **Draft PR** with failure info |
| `fix_or_fail` | Regular PR  | Regular PR                 | No PR, task fails              |
| `fix_or_warn` | Regular PR  | Regular PR                 | **Draft PR** with failure info |

### Fix Loop

For `fix_or_*` policies, when post-task hooks fail:

```
1. Capture hook failure output (test errors, lint failures, etc.)
2. Check if attempts < max_fix_attempts
3. If yes:
   a. Feed failure output back to Claude Code as a new prompt:
      "The following checks failed after your changes. Fix the issues:
       <hook output>"
   b. Run Claude Code again in the same working directory
   c. Run post-task hooks again
   d. Repeat if still failing
4. If no more attempts:
   a. Apply policy (fail → no PR, warn → draft PR)
```

### Pre-commit Auto-Fix Handling

Pre-commit hooks can modify files (auto-formatting, trailing whitespace, import sorting). The flow should handle this:

```
1. Run pre-commit
2. If pre-commit modified files (exit code 1 but files changed):
   a. git add -A
   b. git commit --amend --no-edit   (fold formatting fixes into Claude's commit)
   c. Run remaining post-task hooks (e.g. pytest)
3. If pre-commit failed without fixing (actual errors):
   a. Enter fix loop as normal
```

## PR Creation

### Branch Naming

The branch name is generated as part of the Haiku summary call (see below), with a prefix:

```
wowkmang/{short-descriptive-name}
```

Examples:

- `wowkmang/fix-login-validation-42`
- `wowkmang/refactor-auth-module`
- `wowkmang/cleanup-unused-imports`

### PR Metadata via Haiku Summary Call

After Claude Code finishes, a **separate, lightweight API call** using Claude Haiku generates:

- PR title
- PR description (with `Closes #N` if applicable)
- Branch name

**Input to the summary call:**

- The original task prompt
- The git diff of changes made
- Source information (issue number, PR number, etc.)
- Whether post-task hooks passed or failed (and failure output)

```python
class SummaryGenerator:
    def generate_pr_metadata(
        self,
        task: Task,
        diff: str,
        hook_result: Optional[HookResult],
    ) -> PRMetadata:
        """Generate PR title, description, and branch name using Haiku."""

        prompt = f"""Generate PR metadata for the following changes.

Task: {task.task}
Source: {task.source.type}
{"Issue: #" + str(task.source.issue_number) if task.source.issue_number else ""}
{"PR: #" + str(task.source.pr_number) if task.source.pr_number else ""}

Git diff:
```

{diff[:10000]} # Truncate large diffs

````

{"Post-task hooks FAILED with output:" + hook_result.output if hook_result and not hook_result.success else "All post-task hooks passed."}

Respond in YAML format:
```yaml
title: <concise PR title>
branch: <short-kebab-case-name, no prefix>
description: |
  <PR description>
  <Include "Closes #N" if an issue number is available>
  <If hooks failed, include a collapsible section with failure details>
```"""

        response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )

        # Parse YAML response
        metadata = yaml.safe_load(extract_yaml(response.content[0].text))
        return PRMetadata(
            title=metadata["title"],
            branch=f"wowkmang/{metadata['branch']}",
            description=metadata["description"],
        )
````

### Regular vs Draft PRs

| Condition                                      | PR Type      | Labels                     |
| ---------------------------------------------- | ------------ | -------------------------- |
| Post-task hooks pass                           | Regular PR   | `wowkmang/done`            |
| Post-task hooks fail + warn policy             | **Draft PR** | `wowkmang/needs-attention` |
| Post-task hooks fail + fix succeeds            | Regular PR   | `wowkmang/done`            |
| Post-task hooks fail + fix fails + fix_or_warn | **Draft PR** | `wowkmang/needs-attention` |

Draft PRs include the failure output in a collapsible section in the PR body:

```markdown
<details>
<summary>⚠️ Post-task checks failed</summary>

\`\`\`
FAILED tests/test_login.py::test_invalid_password - AssertionError: ...
FAILED tests/test_login.py::test_empty_email - ValueError: ...

2 failed, 15 passed
\`\`\`

</details>
```

### Issue Linking

- For GitHub-triggered tasks: always include `Closes #N` in the PR description
- For manual tasks: the Haiku summary call infers issue references from the task prompt and diff if possible
- The trigger label on the issue is replaced with the appropriate result label

## GitHub Client

```python
class GitHubClient:
    def __init__(self, token: str, repo: str):
        self.token = token
        self.repo = repo  # "user/project"

    def create_pr(
        self,
        title: str,
        body: str,
        branch: str,
        base: str,
        draft: bool = False,
    ) -> dict:
        """Create a pull request via GitHub API."""
        ...

    def add_labels(self, issue_number: int, labels: list[str]):
        """Add labels to an issue or PR."""
        ...

    def remove_label(self, issue_number: int, label: str):
        """Remove a label from an issue or PR."""
        ...

    def push_branch(self, work_dir: Path, branch: str):
        """Push the working branch to the remote."""
        subprocess.run(
            ["git", "push", "origin", branch],
            cwd=work_dir,
            check=True,
        )
```

## GitHub Webhook Handling

### Event Processing

```python
@app.post("/webhooks/github")
async def github_webhook(request: Request):
    body = await request.body()
    event_type = request.headers.get("x-github-event")
    payload = json.loads(body)

    # Auth is handled by middleware (signature verification)

    if event_type == "issues" and payload.get("action") == "labeled":
        return handle_issue_labeled(payload)
    elif event_type == "pull_request" and payload.get("action") == "labeled":
        return handle_pr_labeled(payload)
    else:
        return {"status": "ignored", "reason": f"Unhandled event: {event_type}/{payload.get('action')}"}
```

### Label Trigger

```python
def handle_issue_labeled(payload: dict):
    label_name = payload["label"]["name"]
    repo_full_name = payload["repository"]["full_name"]
    project = find_project_by_repo(repo_full_name)

    if not project or label_name != project.github_labels.trigger:
        return {"status": "ignored"}

    issue = payload["issue"]
    task = Task(
        project=project.name,
        repo=f"https://github.com/{repo_full_name}",
        ref=project.ref,
        task=f"Fix the issue:\n\nTitle: {issue['title']}\n\n{issue['body'] or ''}",
        source=TaskSourceInfo(
            type=TaskSource.GITHUB_ISSUE,
            issue_number=issue["number"],
            event="labeled",
        ),
    )

    save_task_to_pending(task)
    return {"status": "accepted", "task_id": task.id}
```

## Full Task Lifecycle

Putting it all together — the complete flow from webhook to PR:

```
1. INGESTION
   GitHub webhook fires (issue labeled "wowkmang")
   → POST /webhooks/github
   → Verify X-Hub-Signature-256 against project webhook_secret
   → Create Task from issue title + body
   → Write task YAML to tasks/pending/
   → Return 202 Accepted

2. PICKUP
   Worker thread polls tasks/pending/
   → Finds oldest task file
   → Atomic rename to tasks/running/
   → Load task + project config

3. REPO PREPARATION
   → Fetch/update bare clone in cache/repos/
   → git clone --reference into temp work directory
   → Checkout ref, create wowkmang/* branch

4. PRE-TASK HOOKS
   → Spin up Docker container with work dir mounted
   → Run pre_task commands (e.g. uv sync)
   → If any fail: task fails, move to failed/, stop
   → Container is removed

5. CLAUDE CODE EXECUTION
   → Spin up Claude Code Docker container
   → Mount work dir, dependency caches, credentials
   → Claude Code runs against the task prompt
   → CLAUDE.md from repo + extra_instructions from project config
   → Collect exit code and logs
   → Container is removed

6. POST-TASK HOOKS
   → Spin up container, run post_task commands
   → If pre-commit modified files: amend commit, continue with remaining hooks
   → If all pass: proceed to step 7
   → If fail:
     - Policy is fail → task fails, move to failed/, stop
     - Policy is warn → proceed to step 7 (draft=true)
     - Policy is fix_or_* → enter fix loop:
       a. Feed failure output back to Claude Code
       b. Run Claude Code again (same work dir)
       c. Run post-task hooks again
       d. Repeat up to max_fix_attempts
       e. If still failing:
          - fix_or_fail → task fails
          - fix_or_warn → proceed to step 7 (draft=true)

7. PR METADATA
   → Compute git diff
   → Call Haiku model to generate: title, branch name, description
   → Include "Closes #N" if issue number is known
   → Include failure details in collapsible section if hooks failed

8. PR CREATION
   → Rename branch to the Haiku-generated name
   → Push branch to GitHub
   → Open PR (or draft PR if checks didn't pass)
   → Add appropriate labels to PR and original issue
   → Remove trigger label from issue

9. CLEANUP
   → Update task YAML with result (PR URL, duration, etc.)
   → Move task file to tasks/done/ (or tasks/failed/)
   → Clean up temp work directory
```

## Dependencies

```toml
[project]
name = "wowkmang"
version = "0.1.0"
requires-python = ">=3.12"

dependencies = [
    "fastapi>=0.115",
    "uvicorn>=0.34",
    "pydantic>=2.0",
    "pyyaml>=6.0",
    "docker>=7.0",             # Docker SDK for Python
    "httpx>=0.28",             # For GitHub API calls
    "anthropic>=0.42",         # For Haiku summary calls
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

## Docker Compose

```yaml
services:
  wowkmang:
    build: .
    ports:
      - "8484:8484"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./projects:/app/projects:ro
      - wowkmang-tasks:/app/tasks
      - wowkmang-cache-repos:/app/cache/repos
      - wowkmang-cache-uv:/app/cache/uv
      - wowkmang-cache-pip:/app/cache/pip
      - wowkmang-cache-npm:/app/cache/npm
      - wowkmang-cache-tf:/app/cache/tf
      # Claude Code config (API key)
      - ~/.claude:/home/app/.claude:ro
    environment:
      - WOWKMANG_HOST_DATA_DIR=/var/lib/docker/volumes/wowkmang  # Adjust to match host volume paths
      - WOWKMANG_API_TOKENS=${WOWKMANG_API_TOKENS}
    restart: unless-stopped

volumes:
  wowkmang-tasks:
  wowkmang-cache-repos:
  wowkmang-cache-uv:
  wowkmang-cache-pip:
  wowkmang-cache-npm:
  wowkmang-cache-tf:
```

## Error Handling

### Task Failures

- Pre-task hook failure: task fails immediately, no API credits spent
- Claude Code timeout: container is killed, task fails
- Claude Code non-zero exit: task fails
- Post-task hook failure: governed by policy
- GitHub API failure (PR creation): retry up to 3 times with backoff, then fail
- Docker errors: task fails with error details

### All Errors Recorded

Every failure is recorded in the task YAML's `result.error` field and the task file is moved to `failed/`. This provides a full audit trail in the filesystem.

## Security Considerations

- **Credentials scoped per project**: Each project only has access to its own tokens. Claude Code containers only receive the credentials defined in their project config.
- **Constant-time auth comparison**: Both token and HMAC checks use `hmac.compare_digest()`.
- **Docker socket access**: Inherent to the problem. Run wowkmang on a dedicated machine treated as privileged infrastructure.
- **File permissions**: `projects/` directory and its contents should be readable only by the wowkmang process (mode 700/600).
- **Rate limiting**: Per-token rate limits on the API to prevent runaway task creation and cost overruns.
- **Container resource limits**: CPU and memory limits on Claude Code containers to prevent runaway resource usage.
- **Timeout enforcement**: Hard timeout on Claude Code containers. Killed if exceeded.

## Future Features (v2+)

These are explicitly **out of scope** for v1 but documented for future reference:

- **Web dashboard**: Task status, logs, cost tracking, project management UI
- **Concurrency**: Multiple worker threads/processes, configurable global and per-project concurrency limits to avoid branch conflicts
- **Comment-only output**: For research/analysis tasks where code changes aren't the answer — output is a comment on the issue rather than a PR
- **PR review output**: For `wowkmang` label on PRs — review the PR, address review comments
- **wowkmang self-improvement**: Use wowkmang to build and improve wowkmang itself
- **Cost tracking**: Track API token usage per task, per project
- **Approval gates**: Require human approval before pushing/opening PR
- **Multi-step workflows**: Chain multiple Claude Code invocations for complex tasks
- **Retry with different model**: If a task fails with Sonnet, automatically retry with Opus
- **Slack/Discord notifications**: Report task completion to chat
- **External secret store**: Vault, AWS SSM, etc. instead of plain text in project configs

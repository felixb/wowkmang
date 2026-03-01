# wowkmang — Design Document

> Autonomous task runner for Claude Code. Receives tasks via webhooks, runs Claude Code in isolated Docker containers, and produces pull requests.
>
> Named after the Belter Creole word for "worker" — the one who does the wowk that the bosmang orders.

## Overview

wowkmang is a standalone, self-hosted service that:

1. Receives tasks via webhooks (GitHub events) or direct API calls
1. Queues them as YAML files in a filesystem-based queue
1. Picks them up one at a time with a background worker thread
1. Spins up isolated Docker containers running Claude Code against the target repo
1. Runs pre/post-task hooks (dependency install, tests, linting)
1. Opens pull requests with the results

It is designed to be project-agnostic. Pointing it at a new repo is just adding a project config file and a webhook.

## Architecture

```
Host
+-- Docker daemon
+-- wowkmang container (mounts docker.sock)
|     +-- FastAPI (webhook receiver + manual API)
|     +-- Worker thread (picks up tasks, manages container lifecycle)
+-- Claude Code container (current task) <- sibling, not child
+-- Named Docker volumes
      +-- wowkmang-tasks            (task queue YAML files)
      +-- wowkmang-project-{name}   (per-project: bare repo cache, .claude/, .netrc)
      +-- wowkmang-work-{task_id}   (per-task: fresh clone, removed after task)
```

### Docker: Socket Mount, Not DinD

wowkmang does NOT use Docker-in-Docker. Instead, the host's Docker socket is mounted into the wowkmang container:

```
docker run -v /var/run/docker.sock:/var/run/docker.sock ...
```

Claude Code containers run as **siblings** on the host, not nested inside wowkmang. This is the standard pattern used by CI systems like Jenkins and GitLab Runner.

### Volume Strategy

wowkmang uses **named Docker volumes** (not host-path mounts) for all data shared with Claude Code containers:

| Volume     | Naming                    | Label           | Lifetime                           | Mount point  |
| ---------- | ------------------------- | --------------- | ---------------------------------- | ------------ |
| Task queue | `wowkmang-tasks`          | —               | permanent (compose)                | `/app/tasks` |
| Project    | `wowkmang-project-{name}` | none            | persistent, never auto-deleted     | `/cache`     |
| Work       | `wowkmang-work-{task_id}` | `wowkmang=true` | per-task, removed after completion | `/workspace` |

**Project volumes** persist across tasks for the same project. They hold:

- Bare git clone (reference cache for fast cloning)
- `.claude/credentials.json` (Anthropic API credentials)
- `.netrc` (GitHub token for git auth — never embedded in URLs)
- `.gitignore_global` (excludes `.claude-result.json`)

`HOME=/cache` for all containers, so Claude Code reads credentials from the project volume.

**Work volumes** are created fresh for each task and hold the full git checkout under `/workspace/repo/`. They are removed after the task completes (unless `keep_workdir` is set for debugging).

Only work volumes carry the `wowkmang=true` label. This means `kill_stale_containers()` cleans up orphaned work volumes but never deletes project volumes.

## Project Structure

```
wowkmang/
  wowkmang/
    __init__.py                 # Re-exports `app` from api/routes
    api/
      __init__.py
      routes.py                 # FastAPI app, all endpoints, lifespan
      auth.py                   # Authenticator class, token hashing, HMAC verification
      config.py                 # GlobalConfig, ProjectConfig, GitHubLabels (pydantic-settings)
    executor/
      __init__.py
      worker.py                 # Background worker thread, task pipeline, fix loop
      docker_runner.py          # Container lifecycle, volume management, credential seeding
      repo_cache.py             # Bare-clone + reference-clone strategy (runs inside containers)
      hooks.py                  # Pre/post-task hook execution, pre-commit handling
      github_client.py          # PR creation, labels, comments (PyGithub)
      prompts.py                # Task prompt construction, result file schema
      result_file.py            # Parse .claude-result.json (TaskOutput, CommitInfo, etc.)
      summary.py                # Fallback PR metadata generation (no API call)
    taskqueue/
      __init__.py
      models.py                 # Task, TaskResult, TaskSource, TaskStatus
      task_queue.py             # Filesystem queue operations (save, pick, complete, fail, wait)
  example/
    projects/
      myptoject.yaml            # Annotated example project config
  projects/                     # Per-project config YAML files (gitignored)
  tasks/                        # Filesystem-based task queue
    pending/
    running/
    done/
    failed/
    waiting/
    context/
  Dockerfile
  docker-compose.yaml
  pyproject.toml
```

## Configuration

### Global Configuration

Global settings are loaded from environment variables (prefix `WOWKMANG_`) and `.env` file via pydantic-settings.

| Field                    | Default                                 | Description                                         |
| ------------------------ | --------------------------------------- | --------------------------------------------------- |
| `host_claude_config_dir` | `""`                                    | Host path to `~/.claude` (for credential injection) |
| `projects_dir`           | `./projects`                            | Path to project config directory                    |
| `tasks_dir`              | `./tasks`                               | Path to task queue directory                        |
| `host`                   | `0.0.0.0`                               | Bind address                                        |
| `port`                   | `8484`                                  | Port                                                |
| `api_tokens`             | `""`                                    | Comma-separated SHA-256 hashes of bearer tokens     |
| `pull_token`             | `""`                                    | Token for pulling Docker images                     |
| `github_token`           | `""`                                    | Global fallback GitHub token                        |
| `docker_image`           | `ghcr.io/anthropics/claude-code:latest` | Default Claude Code image                           |
| `container_uid`          | `1000:1000`                             | Default UID:GID for containers                      |
| `keep_workdir`           | `false`                                 | Keep work volume after task (debugging)             |
| `task_retention_days`    | `7`                                     | Days to keep done/failed task files                 |
| `git_name`               | `wowkmang`                              | Git user.name for commits                           |
| `git_email`              | `wowkmang@noreply`                      | Git user.email for commits                          |
| `log_level`              | `info`                                  | Logging level                                       |

### Project Configuration

Each project is defined by a YAML file in `projects/`.

```yaml
name: myproject
repo: https://github.com/user/project
ref: main

github_token: ghp_xxxxxxxxxxxxxxxxxxxx

default_model: sonnet
extra_instructions: |
  Always write tests for new functionality.

docker_image: ghcr.io/anthropics/claude-code:latest
container_uid: "1000:1000"
timeout_minutes: 30
max_fix_attempts: 2

pre_task:
  - uv sync

post_task:
  - uv run pytest

post_task_policy: fix_or_warn

github_labels:
  trigger: wowkmang
  in_progress: wowkmang/in-progress
  done: wowkmang/done
  failed: wowkmang/failed
  needs_attention: wowkmang/needs-attention

webhook_secret: whsec_xxxxxxxxxxxx
```

### File Permissions

The `projects/` directory contains secrets and must be readable only by the wowkmang process:

```bash
chmod 700 projects/
chmod 600 projects/*.yaml
```

## Authentication

Two authentication mechanisms, checked per-request based on which headers are present:

### 1. Bearer Token Auth (API calls)

```
Authorization: Bearer <token>
```

- Tokens are configured via `WOWKMANG_API_TOKENS` environment variable
- Stored and compared as SHA-256 hashes
- Uses `hmac.compare_digest()` for constant-time comparison
- Generate a hash: `echo -n "your-token" | python -m wowkmang.api.auth`

### 2. GitHub Webhook Signature (GitHub events)

```
X-Hub-Signature-256: sha256=<hex_digest>
```

- Each project config has a `webhook_secret`
- Verifies HMAC-SHA256 of the raw request body against the project's secret
- Uses `hmac.compare_digest()` for constant-time comparison
- The project is identified from the webhook payload (`repository.full_name`)

## Task Queue

### Filesystem-Based Queue

Tasks are stored as individual YAML files, organized into directories by state:

```
tasks/
  pending/          # Waiting to be picked up
  running/          # Currently being processed
  done/             # Completed successfully
  failed/           # Failed after all attempts
  waiting/          # Paused, awaiting user answers
  context/          # Fetched GitHub comments (JSON files)
```

### State Transitions

State transitions are **file renames** (`os.rename()`), which are atomic on the same filesystem:

```
pending/ -> running/ -> done/
                     -> failed/
                     -> waiting/ -> pending/ (resumed with answers)
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
project: myproject
repo: https://github.com/user/project
ref: main
task: "Fix the login bug described in issue #42"
model: opus                                   # Optional override, falls back to project default
source:
  type: github_issue                          # or: github_pr, manual, api
  issue_number: 42
  pr_number: null
  event: labeled
attempts: 0
max_attempts: 3
comments_file: context/a1b2c3d4_comments.json # Path to fetched comments, if any
allow_questions: false                         # Whether Claude can ask clarifying questions
pr_branch: null                                # Existing PR branch (PR-triggered tasks)

# Result — appended by worker after completion
result:
  status: completed                            # or: failed, timeout, waiting_for_input
  pr_url: https://github.com/user/project/pull/87
  pr_number: 87
  branch: wowkmang/fix-login-bug
  duration_seconds: 342
  finished: "2025-02-16T14:37:42Z"
  post_task_passed: false
  fix_attempts: 2
  error: null
  logs: |                                      # Contents of .wowkmang/steps.log
    ...
  questions:                                   # Questions Claude asked (if allow_questions)
    - message: "Which auth method should I use?"
      choices: ["JWT", "OAuth"]
```

## API Endpoints

### GET /health

Unauthenticated health check. Returns worker status and queue depth when authenticated.

```json
{"status": "ok", "worker": "idle", "queue_depth": 3}
```

### POST /tasks

Manual task creation. Authenticated via Bearer token.

**Request body:**

```json
{
  "project": "myproject",
  "task": "Fix the login bug described in issue #42",
  "ref": "main",
  "model": "opus",
  "allow_questions": true
}
```

Only `project` and `task` are required. `ref` and `model` fall back to project defaults.

**Response:** `202 Accepted`

```json
{"id": "a1b2c3d4", "status": "pending", "project": "myproject"}
```

### GET /tasks

List tasks. Authenticated via Bearer token. Optional `?status=` filter (pending, running, done, failed, waiting).

### GET /tasks/{task_id}

Returns current task status and result. Authenticated via Bearer token.

### GET /tasks/{task_id}/questions

Returns the questions from a task in `waiting` status. Authenticated via Bearer token.

### POST /tasks/{task_id}/answers

Submit answers to a waiting task. Authenticated via Bearer token.

**Request body:**

```json
{"answers": ["Use JWT", "Yes"]}
```

The task is moved from `waiting/` back to `pending/` for the worker to resume.

### POST /webhooks/github

Receives GitHub webhook events. Authenticated via `X-Hub-Signature-256`.

**Handled events:**

- `issues` + `labeled` (trigger label): creates task from issue title + body, fetches comments
- `pull_request` + `labeled` (trigger label): creates task from PR title + body, records existing `pr_branch`, fetches review comments
- `issue_comment` + `created`: resumes a waiting task if one exists for the source issue/PR (answers bypass trigger label)

**Response:** `202 Accepted` with task ID, or `200 OK` if event is irrelevant.

## Worker

### Single-Threaded Background Worker

The worker runs as a daemon thread in the FastAPI process, polling `pending/` every 5 seconds. It is started/stopped via the FastAPI lifespan.

### Task Processing Pipeline

```
_process_task(task_file):
  1. Load task YAML and project config
  2. Create work volume: wowkmang-work-{task_id} (labeled wowkmang=true)
  3. Get/create project volume: wowkmang-project-{project_name} (no label)
  4. Remove trigger label from source issue/PR
  5. Setup environment:
     a. Pull Docker image (ensure_image)
     b. Seed credentials.json from host into project volume (/cache/.claude/)
     c. Write .netrc to project volume (/cache/.netrc) for git auth
     d. Chown both volumes to container_uid
     e. Set up global gitignore (excludes .claude-result.json)
     f. Create /workspace/.wowkmang/ log directory
  6. Clone repo into work volume (using bare-clone reference cache)
  7. Configure git user.name/email in the cloned repo
  8. Run pre_task hooks
     -> If any fail: task fails, stop
  9. Run Claude Code:
     claude --dangerously-skip-permissions --model {model} --print {prompt}
  10. Read .claude-result.json from work volume
  11. Run post-task checks:
      a. If .pre-commit-config.yaml exists: run pre-commit twice
         (first pass auto-fixes, git add -A, second pass verifies)
      b. Run post_task hooks
      c. Apply post_task_policy (see hooks section)
  12. If no changes produced:
      - Post comment if result file has one
      - If questions and allow_questions: move to waiting/
      - Otherwise: complete with "No changes produced"
  13. If changes exist: publish
      a. Get PR metadata from .claude-result.json commit field
         (or fallback: truncated task text, slugified branch name)
      b. git add -A && git commit
      c. Rename branch to wowkmang/{branch_name} (unless existing PR branch)
      d. git push origin {branch}
      e. Post comment to issue/PR if result file has one
      f. Create PR (or skip if using existing PR branch)
      g. Add done/needs_attention label
      h. If questions and allow_questions: move to waiting/
      i. Otherwise: complete task
  14. Cleanup:
      - Save steps.log to task.result.logs
      - Remove work volume (unless keep_workdir)
```

### PR Metadata

PR metadata (title, description, branch name) comes from Claude Code itself. The task prompt instructs Claude to write a `.claude-result.json` file:

```json
{
  "commit": {
    "title": "Fix login validation for empty emails",
    "description": "Closes #42\n\nAdded email format validation...",
    "branch_name": "fix-login-validation"
  },
  "comment": {
    "message": "I've fixed the issue. The root cause was..."
  },
  "questions": [
    {"message": "Which auth method?", "choices": ["JWT", "OAuth"]}
  ]
}
```

All fields are optional. If `commit` is missing, `fallback_metadata()` generates a title and branch from the task text (no API call).

### Crash Recovery

On startup, the worker runs `_recover_stale_tasks()`:

1. Kill orphaned containers and labeled volumes (`kill_stale_containers()`)
1. List all files in `running/`
1. If `attempts < max_attempts`: move back to `pending/` for retry
1. Otherwise: move to `failed/`

## Pre/Post-Task Hooks

### Execution

Hooks run inside Docker containers with the same image and volume setup as Claude Code. Pre-task hooks run before Claude Code; post-task hooks run after.

### Pre-commit Handling

If the repo has a `.pre-commit-config.yaml`, the worker runs pre-commit as part of post-task checks:

1. `pre-commit run -a` (first pass — may auto-fix files)
1. `git add -A` (stage auto-fixes)
1. `pre-commit run -a` (second pass — verify all clean)
1. If second pass fails: return failure

This runs before the project's `post_task` commands. Only `git add -A` is used to stage fixes — no commit amending.

### Post-Task Policies

| Policy        | Checks Pass | Fix Succeeds | Fix Fails                      |
| ------------- | ----------- | ------------ | ------------------------------ |
| `fail`        | Regular PR  | N/A          | No PR, task fails              |
| `warn`        | Regular PR  | N/A          | **Draft PR** with failure info |
| `fix_or_fail` | Regular PR  | Regular PR   | No PR, task fails              |
| `fix_or_warn` | Regular PR  | Regular PR   | **Draft PR** with failure info |

### Fix Loop

For `fix_or_*` policies, when post-task hooks fail:

1. Feed failure output back to Claude Code as a continuation prompt
1. Run `claude --continue` in the same work directory
1. Run post-task hooks again
1. Repeat up to `max_fix_attempts` times
1. If still failing: apply policy (`fix_or_fail` -> task fails, `fix_or_warn` -> draft PR)

The fix loop always uses `--continue` (continue session) so Claude has context from the previous run. `extra_instructions` are not re-injected on continuation runs.

## Docker Runner

### Container Settings

All containers share these settings:

- `HOME=/cache` (project volume)
- `GIT_DISCOVERY_ACROSS_FILESYSTEM=1`
- `mem_limit=4g`, 2 CPUs
- `entrypoint=""` (overridden)
- Label: `wowkmang=true`
- `working_dir=/workspace/repo`

### Credential Injection

- `seed_credentials()`: copies `credentials.json` from host `WOWKMANG_HOST_CLAUDE_CONFIG_DIR` into the project volume at `/cache/.claude/credentials.json`
- `setup_netrc()`: writes a `.netrc` file to `/cache/.netrc` with the project's GitHub token for git authentication. The token is passed via environment variable to avoid leaking in shell command arguments.

### Image Management

`ensure_image()` pulls the Docker image, trying authentication in order:

1. `pull_token` (if configured)
1. Project `github_token`
1. Unauthenticated

Images are tracked per session to avoid redundant pulls.

### Stale Cleanup

`kill_stale_containers()` removes all containers and **labeled** volumes (`wowkmang=true`). Project volumes have no label and are never auto-deleted.

## Repo Cache

The repo cache runs **inside containers** (not on the host), using the project volume for persistent storage.

### Strategy: Bare Clone + `--reference`

```
/cache/{sanitized_url}/     <- bare mirror (updated via fetch --all each task)
/workspace/repo/            <- full clone using --reference (fast, local objects)
```

Cache subdir name: repo URL with `https://` stripped, `/` -> `_`, `.git` stripped.
Example: `github.com_user_project`.

For new tasks:

1. If cache exists: `git fetch --all`
1. Else: `git clone --bare {url} /cache/{subdir}`
1. `git clone --reference /cache/{subdir} {url} /workspace/repo`
1. `git checkout -b wowkmang/{8-char-uuid} origin/{ref}`

For PR-triggered tasks: same steps 1-3, then `git checkout {existing_branch}` instead.

## GitHub Client

Uses **PyGithub** for all GitHub API interactions.

```python
class GitHubClient:
    def __init__(self, token: str, repo: str):
        self._repo = Github(auth=Auth.Token(token)).get_repo(repo)
```

Methods:

- `create_pr(title, body, branch, base, draft)` — create pull request
- `add_labels(issue_number, labels)` — add labels to issue/PR
- `remove_label(issue_number, label)` — remove label (silently ignores 404)
- `get_issue_comments(issue_number)` — fetch issue comments
- `get_pr_comments(pr_number)` — fetch PR comments + review comments
- `get_pr_branch(pr_number)` — get head branch name
- `create_comment(issue_number, body)` — post a comment

### Comment Fetching

`fetch_and_save_comments()` is a standalone function that fetches comments for an issue or PR and saves them as `{task_id}_comments.json` in the task queue's `context/` directory. The path is stored in `task.comments_file` and the comments are included in the task prompt sent to Claude Code.

## Prompts

`build_task_prompt()` assembles the full prompt sent to Claude Code:

1. CI environment preamble (no interactive tools available)
1. Result file schema — JSON schema for `.claude-result.json` that Claude should write
1. Questions instructions (allowed or not, based on `task.allow_questions`)
1. Source reference (`GitHub issue #N` or `GitHub PR #N`)
1. Fetched issue/PR comments (if any)
1. The task text itself

`extra_instructions` from the project config are prepended by the Docker runner on the initial run only (not on `--continue` fix iterations).

## Dependencies

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn>=0.34",
    "pydantic>=2.0",
    "pydantic-settings>=2.0",
    "PyYAML>=6.0",
    "docker>=7.0",
    "PyGithub>=2.8.1",
]
```

No `anthropic` SDK — wowkmang does not call the Anthropic API directly. All Claude interaction is via the Claude Code CLI inside Docker containers.

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
    environment:
      - WOWKMANG_HOST_CLAUDE_CONFIG_DIR=${HOME}/.claude
      - WOWKMANG_API_TOKENS=${WOWKMANG_API_TOKENS}
    restart: unless-stopped

volumes:
  wowkmang-tasks:
```

Only the task queue is declared as a compose volume. Project and work volumes are created on demand by the Docker runner as named Docker volumes.

## Error Handling

### Task Failures

- Pre-task hook failure: task fails immediately, no API credits spent
- Claude Code timeout: container is killed, task fails
- Claude Code non-zero exit: task fails
- Post-task hook failure: governed by policy
- Docker errors: task fails with error details

### All Errors Recorded

Every failure is recorded in the task YAML's `result.error` field and the task file is moved to `failed/`. The `result.logs` field captures the step log for debugging.

## Security Considerations

- **Constant-time auth comparison**: Both token and HMAC checks use `hmac.compare_digest()`.
- **File permissions**: `projects/` directory and its contents should be readable only by the wowkmang process (mode 700/600).
- **Credential isolation**: GitHub tokens are injected via `.netrc` (never in URLs or shell args). Anthropic credentials are seeded into the project volume per-task.
- **Container resource limits**: CPU and memory limits on all containers.
- **Timeout enforcement**: Hard timeout on Claude Code containers, killed if exceeded.
- **Labeled cleanup**: Only work volumes carry the `wowkmang` label, so stale cleanup never deletes persistent project data.

### Docker Socket Access

wowkmang requires access to the Docker daemon socket (`/var/run/docker.sock`). **Any process with Docker socket access has effective root privileges on the host.** This is an inherent requirement of the architecture, same as CI systems like Jenkins and GitLab Runner.

**Deployment guidance:**

- **Dedicated host or VM**: Run wowkmang on a machine treated as privileged infrastructure.
- **Docker socket proxy**: Use a restricting proxy like [Tecnativa/docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) to limit which Docker API endpoints wowkmang can access.
- **Minimal host footprint**: No other services, secrets, or sensitive data on the host.
- **Network segmentation**: Place the wowkmang host in a separate network segment.
- **Token scoping**: Use fine-grained GitHub PATs with minimal permissions.

## Future Features

- **Web dashboard**: Task status, logs, cost tracking, project management UI
- **Concurrency**: Multiple worker threads, per-project concurrency limits
- **Cost tracking**: Track API token usage per task, per project
- **Slack/Discord notifications**: Report task completion to chat
- **External secret store**: Vault, AWS SSM, etc. instead of plain text in project configs

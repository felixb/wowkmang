# wowkmang

> Autonomous task runner for Claude Code. Receives tasks via webhooks, runs Claude Code in isolated Docker containers, and produces pull requests.
>
> Named after the Belter Creole word for "worker" — the one who does the wowk that the bosmang orders.

## Overview

wowkmang is a self-hosted service that:

1. Receives tasks via webhooks (GitHub events) or direct API calls
1. Queues them as YAML files in a filesystem-based task queue
1. Picks them up one at a time with a background worker thread
1. Spins up isolated Docker containers running Claude Code against the target repo
1. Runs pre/post-task hooks (dependency install, tests, linting)
1. Opens pull requests with the results

Pointing it at a new repo is just adding a project config file and a webhook.

## Quick Start

### Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- Docker

### Install

```bash
git clone https://github.com/you/wowkmang
cd wowkmang
uv sync --all-extras
```

### Configure

1. Create a project config in `projects/`:

```bash
mkdir -p projects
cp example/projects/myptoject.yaml projects/myproject.yaml
# edit projects/myproject.yaml
chmod 700 projects && chmod 600 projects/*.yaml
```

2. Set environment variables (or create a `.env` file):

```bash
export WOWKMANG_HOST_CLAUDE_CONFIG_DIR=~/.claude   # Host path to Claude config (for credentials)
export WOWKMANG_API_TOKENS=$(echo -n "your-secret-token" | uv run python -m wowkmang.api.auth)
```

### Run

```bash
uv run uvicorn wowkmang:app --host 0.0.0.0 --port 8484
```

### Docker

```bash
docker compose up
```

## Project Configuration

Each project is a YAML file in `projects/`:

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

post_task_policy: fix_or_warn   # fail | warn | fix_or_fail | fix_or_warn

github_labels:
  trigger: wowkmang

webhook_secret: whsec_xxxxxxxxxxxx
```

## Authentication

### API tokens

Generate a token hash to store in `WOWKMANG_API_TOKENS`:

```bash
echo -n "your-token" | python -m wowkmang.api.auth
```

`WOWKMANG_API_TOKENS` accepts a comma-separated list of SHA-256 hashes.

Use the token in API requests:

```
Authorization: Bearer your-token
```

### GitHub webhooks

Each project config has a `webhook_secret`. GitHub signs webhook payloads with HMAC-SHA256 — wowkmang verifies the `X-Hub-Signature-256` header on every webhook request.

## API

| Method | Path                    | Auth            | Description                                                    |
| ------ | ----------------------- | --------------- | -------------------------------------------------------------- |
| `GET`  | `/health`               | optional bearer | Health check; queue depth shown when authenticated             |
| `POST` | `/tasks`                | bearer          | Create a task manually                                         |
| `GET`  | `/tasks`                | bearer          | List tasks (`?status=pending\|running\|done\|failed\|waiting`) |
| `GET`  | `/tasks/{id}`           | bearer          | Get task status                                                |
| `GET`  | `/tasks/{id}/questions` | bearer          | Get questions from a waiting task                              |
| `POST` | `/tasks/{id}/answers`   | bearer          | Submit answers to resume a waiting task                        |
| `POST` | `/webhooks/github`      | webhook sig     | Receive GitHub label and comment events                        |

### Create a task

```bash
curl -X POST http://localhost:8484/tasks \
  -H "Authorization: Bearer your-token" \
  -H "Content-Type: application/json" \
  -d '{"project": "myproject", "task": "Fix the login bug described in issue #42"}'
```

Optional fields: `ref`, `model`, `allow_questions`.

Response:

```json
{"id": "a1b2c3d4", "status": "pending", "project": "myproject"}
```

### Questions and answers

When a task has `allow_questions: true`, Claude Code can pause and ask clarifying questions. The task moves to `waiting` status. Retrieve questions and submit answers:

```bash
# Get questions
curl http://localhost:8484/tasks/a1b2c3d4/questions \
  -H "Authorization: Bearer your-token"

# Submit answers
curl -X POST http://localhost:8484/tasks/a1b2c3d4/answers \
  -H "Authorization: Bearer your-token" \
  -H "Content-Type: application/json" \
  -d '{"answers": ["Use JWT tokens", "Yes, add rate limiting"]}'
```

For GitHub-triggered tasks, questions are posted as issue comments and answers can be submitted by replying on the issue.

### GitHub webhook trigger

Add a webhook to your GitHub repo pointing at `https://your-host/webhooks/github`. When an issue or PR is labeled with the project's trigger label (default: `wowkmang`), a task is created automatically.

## Task Queue

Tasks are YAML files in `tasks/`:

```
tasks/
  pending/        <- waiting to be picked up
  running/        <- currently processing
  done/           <- completed
  failed/         <- failed after all attempts
  waiting/        <- paused, awaiting user answers
  context/        <- fetched GitHub comments (JSON)
```

State transitions are atomic file renames on the same filesystem.

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

Project and work volumes are created on demand by the Docker runner as named Docker volumes — they are not declared in compose.

## Development

```bash
uv run pytest                              # run tests
uv run uvicorn wowkmang:app --reload       # dev server
```

## License

GPL-3.0 — see [COPYING](LICENSE).

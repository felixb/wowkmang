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
cp projects/example.yaml projects/myproject.yaml
# edit projects/myproject.yaml
chmod 700 projects && chmod 600 projects/*.yaml
```

2. Set environment variables:

```bash
export WOWKMANG_HOST_DATA_DIR=/opt/wowkmang   # Host path prefix for Docker volume mounts
export WOWKMANG_API_TOKENS=$(echo -n "your-secret-token" | uv run python -m wowkmang.auth)
```

### Run

```bash
uv run uvicorn wowkmang.api:app --host 0.0.0.0 --port 8484
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

default_model: claude-sonnet-4-5-20250929
extra_instructions: |
  Always write tests for new functionality.

docker_image: ghcr.io/anthropics/claude-code:latest
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
echo -n "your-token" | python -m wowkmang.auth
```

`WOWKMANG_API_TOKENS` accepts a comma-separated list of SHA-256 hashes.

Use the token in API requests:

```
Authorization: Bearer your-token
```

### GitHub webhooks

Each project config has a `webhook_secret`. GitHub signs webhook payloads with HMAC-SHA256 — wowkmang verifies the `X-Hub-Signature-256` header on every webhook request.

## API

| Method | Path               | Auth        | Description                                           |
| ------ | ------------------ | ----------- | ----------------------------------------------------- |
| `GET`  | `/health`          | none        | Health check and queue depth                          |
| `POST` | `/tasks`           | bearer      | Create a task manually                                |
| `GET`  | `/tasks`           | bearer      | List tasks (`?status=pending\|running\|done\|failed`) |
| `GET`  | `/tasks/{id}`      | bearer      | Get task status                                       |
| `POST` | `/webhooks/github` | webhook sig | Receive GitHub label events                           |

### Create a task

```bash
curl -X POST http://localhost:8484/tasks \
  -H "Authorization: Bearer your-token" \
  -H "Content-Type: application/json" \
  -d '{"project": "myproject", "task": "Fix the login bug described in issue #42"}'
```

Response:

```json
{"id": "a1b2c3d4", "status": "pending", "project": "myproject"}
```

### GitHub webhook trigger

Add a webhook to your GitHub repo pointing at `https://your-host/webhooks/github`. When an issue or PR is labeled with the project's trigger label (default: `wowkmang`), a task is created automatically.

## Task Queue

Tasks are YAML files in `tasks/`:

```
tasks/
  pending/    ← waiting to be picked up
  running/    ← currently processing
  done/       ← completed
  failed/     ← failed after all attempts
```

State transitions are atomic file renames on the same filesystem.

## Development

```bash
uv run pytest          # run tests
uv run uvicorn wowkmang.api:app --reload   # dev server
```

## License

GPL-3.0 — see [COPYING](LICENSE).

import hashlib
import hmac as hmac_mod
import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import SAMPLE_PROJECT, TEST_API_TOKEN, TEST_API_TOKEN_HASH
from wowkmang.api.routes import app, config, projects, authenticator
import wowkmang.api.routes as api_module
from wowkmang.api.config import load_projects, GlobalConfig
from wowkmang.taskqueue.task_queue import ensure_queue_dirs


@pytest.fixture(autouse=True)
def setup_api(global_config, tmp_projects_dir, tmp_tasks_dir):
    api_module.config = global_config
    api_module.projects = load_projects(tmp_projects_dir)
    from wowkmang.api.auth import Authenticator

    api_module.authenticator = Authenticator(global_config, api_module.projects)
    ensure_queue_dirs(global_config.tasks_dir)
    yield


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {TEST_API_TOKEN}"}


class TestHealth:
    def test_health_unauthenticated(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"status": "ok"}

    def test_health_authenticated(self, client, auth_headers):
        resp = client.get("/health", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "queue_depth" in data
        assert "worker" in data


class TestCreateTask:
    def test_create_task(self, client, auth_headers):
        resp = client.post(
            "/tasks",
            json={"project": "testproject", "task": "Fix the bug"},
            headers=auth_headers,
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "pending"
        assert data["project"] == "testproject"
        assert "id" in data

    def test_create_task_unknown_project(self, client, auth_headers):
        resp = client.post(
            "/tasks",
            json={"project": "nope", "task": "Fix it"},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_create_task_no_auth(self, client):
        resp = client.post(
            "/tasks",
            json={"project": "testproject", "task": "Fix"},
        )
        assert resp.status_code == 401


class TestGetTasks:
    def test_list_empty(self, client, auth_headers):
        resp = client.get("/tasks", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_after_create(self, client, auth_headers):
        client.post(
            "/tasks",
            json={"project": "testproject", "task": "A"},
            headers=auth_headers,
        )
        resp = client.get("/tasks", headers=auth_headers)
        assert len(resp.json()) == 1

    def test_get_by_id(self, client, auth_headers):
        create_resp = client.post(
            "/tasks",
            json={"project": "testproject", "task": "B"},
            headers=auth_headers,
        )
        task_id = create_resp.json()["id"]
        resp = client.get(f"/tasks/{task_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["task"] == "B"

    def test_get_not_found(self, client, auth_headers):
        resp = client.get("/tasks/nonexistent", headers=auth_headers)
        assert resp.status_code == 404


class TestCreateTaskWithQuestions:
    def test_create_task_with_allow_questions(self, client, auth_headers):
        resp = client.post(
            "/tasks",
            json={
                "project": "testproject",
                "task": "Investigate the bug",
                "allow_questions": True,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 202
        task_id = resp.json()["id"]

        # Verify the task has allow_questions set
        resp = client.get(f"/tasks/{task_id}", headers=auth_headers)
        assert resp.json()["allow_questions"] is True

    def test_default_allow_questions_is_false(self, client, auth_headers):
        resp = client.post(
            "/tasks",
            json={"project": "testproject", "task": "Fix it"},
            headers=auth_headers,
        )
        task_id = resp.json()["id"]

        resp = client.get(f"/tasks/{task_id}", headers=auth_headers)
        # allow_questions defaults to False, but since it's False it might be excluded
        # The field should not be True
        data = resp.json()
        assert data.get("allow_questions", False) is False


class TestQuestionsEndpoints:
    def test_get_questions_no_result(self, client, auth_headers):
        """Task with no result returns empty questions."""
        resp = client.post(
            "/tasks",
            json={"project": "testproject", "task": "Do something"},
            headers=auth_headers,
        )
        task_id = resp.json()["id"]

        resp = client.get(f"/tasks/{task_id}/questions", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == {"questions": []}

    def test_get_questions_not_found(self, client, auth_headers):
        resp = client.get("/tasks/nonexistent/questions", headers=auth_headers)
        assert resp.status_code == 404

    def test_post_answers_not_found(self, client, auth_headers):
        resp = client.post(
            "/tasks/nonexistent/answers",
            json={"answers": ["yes"]},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_post_answers_task_not_waiting(self, client, auth_headers):
        """Posting answers to a task that isn't waiting returns 400."""
        resp = client.post(
            "/tasks",
            json={"project": "testproject", "task": "Do it"},
            headers=auth_headers,
        )
        task_id = resp.json()["id"]

        resp = client.post(
            f"/tasks/{task_id}/answers",
            json={"answers": ["yes"]},
            headers=auth_headers,
        )
        assert resp.status_code == 400


class TestPRWebhookBranch:
    """Test that PR webhook sets pr_branch from payload."""

    def _sign(self, body: bytes, secret: str) -> str:
        digest = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    def test_pr_labeled_stores_branch(self, client, auth_headers):
        payload = {
            "action": "labeled",
            "label": {"name": "wowkmang"},
            "repository": {"full_name": "user/testproject"},
            "pull_request": {
                "number": 7,
                "title": "PR title",
                "body": "PR body",
                "head": {"ref": "feature/my-branch"},
            },
        }
        body = json.dumps(payload).encode()
        sig = self._sign(body, "whsec_testsecret")
        resp = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "x-hub-signature-256": sig,
                "x-github-event": "pull_request",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        # Verify the task has pr_branch set
        resp = client.get(f"/tasks/{task_id}", headers=auth_headers)
        assert resp.json()["pr_branch"] == "feature/my-branch"


class TestCommentWebhook:
    """Test that issue_comment events resume waiting tasks."""

    def _sign(self, body: bytes, secret: str) -> str:
        digest = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    def _create_waiting_task(self, global_config, issue_number=42):
        from wowkmang.taskqueue.models import (
            Task,
            TaskResult,
            TaskStatus,
            TaskSourceInfo,
            TaskSource,
            task_to_yaml,
        )
        from wowkmang.taskqueue.task_queue import save_task, QueueDir

        task = Task(
            project="testproject",
            repo="https://github.com/user/testproject",
            task="Fix the bug",
            source=TaskSourceInfo(
                type=TaskSource.GITHUB_ISSUE,
                issue_number=issue_number,
            ),
            allow_questions=True,
        )
        task.result = TaskResult(
            status=TaskStatus.WAITING_FOR_INPUT,
            questions=[{"message": "Which approach?", "choices": ["A", "B"]}],
        )
        # Save to waiting dir
        waiting_dir = global_config.tasks_dir / "waiting"
        waiting_dir.mkdir(exist_ok=True)
        ts = task.created.strftime("%Y-%m-%dT%H-%M-%S")
        path = waiting_dir / f"{ts}_{task.id}.yaml"
        path.write_text(task_to_yaml(task))
        return task

    def test_comment_resumes_waiting_task(self, client, global_config):
        task = self._create_waiting_task(global_config, issue_number=42)

        payload = {
            "action": "created",
            "repository": {"full_name": "user/testproject"},
            "issue": {"number": 42},
            "comment": {"body": "Use approach A"},
        }
        body = json.dumps(payload).encode()
        sig = self._sign(body, "whsec_testsecret")
        resp = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "x-hub-signature-256": sig,
                "x-github-event": "issue_comment",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "resumed"
        assert data["task_id"] == task.id

        # Verify task moved back to pending
        pending_dir = global_config.tasks_dir / "pending"
        pending_files = list(pending_dir.glob(f"*_{task.id}.yaml"))
        assert len(pending_files) == 1

    def test_comment_ignored_when_no_waiting_task(self, client):
        payload = {
            "action": "created",
            "repository": {"full_name": "user/testproject"},
            "issue": {"number": 999},
            "comment": {"body": "Hello"},
        }
        body = json.dumps(payload).encode()
        sig = self._sign(body, "whsec_testsecret")
        resp = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "x-hub-signature-256": sig,
                "x-github-event": "issue_comment",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "ignored"


class TestGithubWebhook:
    def _sign(self, body: bytes, secret: str) -> str:
        digest = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    def test_issue_labeled(self, client):
        payload = {
            "action": "labeled",
            "label": {"name": "wowkmang"},
            "repository": {"full_name": "user/testproject"},
            "issue": {
                "number": 42,
                "title": "Bug title",
                "body": "Bug description",
            },
        }
        body = json.dumps(payload).encode()
        sig = self._sign(body, "whsec_testsecret")
        resp = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "x-hub-signature-256": sig,
                "x-github-event": "issues",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert "task_id" in data

    def test_pr_labeled(self, client):
        payload = {
            "action": "labeled",
            "label": {"name": "wowkmang"},
            "repository": {"full_name": "user/testproject"},
            "pull_request": {
                "number": 7,
                "title": "PR title",
                "body": "PR body",
            },
        }
        body = json.dumps(payload).encode()
        sig = self._sign(body, "whsec_testsecret")
        resp = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "x-hub-signature-256": sig,
                "x-github-event": "pull_request",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"

    def test_wrong_label_ignored(self, client):
        payload = {
            "action": "labeled",
            "label": {"name": "other-label"},
            "repository": {"full_name": "user/testproject"},
            "issue": {"number": 1, "title": "T", "body": ""},
        }
        body = json.dumps(payload).encode()
        sig = self._sign(body, "whsec_testsecret")
        resp = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "x-hub-signature-256": sig,
                "x-github-event": "issues",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "ignored"

    def test_invalid_signature(self, client):
        payload = {
            "action": "labeled",
            "label": {"name": "wowkmang"},
            "repository": {"full_name": "user/testproject"},
            "issue": {"number": 1, "title": "T", "body": ""},
        }
        body = json.dumps(payload).encode()
        resp = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "x-hub-signature-256": "sha256=invalid",
                "x-github-event": "issues",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 401

    def test_missing_signature(self, client):
        resp = client.post(
            "/webhooks/github",
            content=b"{}",
            headers={"x-github-event": "issues"},
        )
        assert resp.status_code == 401

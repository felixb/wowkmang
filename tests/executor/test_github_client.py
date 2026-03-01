import json

import httpx
import pytest
import respx

from wowkmang.executor.github_client import GitHubClient, fetch_and_save_comments

BASE_URL = "https://api.github.com/repos/user/project"


@pytest.fixture
def client():
    return GitHubClient(token="ghp_test123", repo="user/project")


class TestCreatePR:
    @respx.mock
    def test_creates_pr_with_correct_payload(self, client):
        route = respx.post(f"{BASE_URL}/pulls").mock(
            return_value=httpx.Response(
                201,
                json={
                    "number": 42,
                    "html_url": "https://github.com/user/project/pull/42",
                },
            )
        )

        result = client.create_pr(
            title="Fix login bug",
            body="Closes #10",
            branch="wowkmang/fix-login",
            base="main",
        )

        assert route.called
        request_json = route.calls[0].request.read()
        import json

        payload = json.loads(request_json)
        assert payload == {
            "title": "Fix login bug",
            "body": "Closes #10",
            "head": "wowkmang/fix-login",
            "base": "main",
            "draft": False,
        }
        assert result["number"] == 42

    @respx.mock
    def test_creates_draft_pr(self, client):
        route = respx.post(f"{BASE_URL}/pulls").mock(
            return_value=httpx.Response(201, json={"number": 43, "draft": True})
        )

        result = client.create_pr(
            title="WIP: fix",
            body="Draft",
            branch="wowkmang/wip",
            base="main",
            draft=True,
        )

        import json

        payload = json.loads(route.calls[0].request.read())
        assert payload["draft"] is True
        assert result["draft"] is True

    @respx.mock
    def test_http_error_raises(self, client):
        respx.post(f"{BASE_URL}/pulls").mock(
            return_value=httpx.Response(422, json={"message": "Validation Failed"})
        )

        with pytest.raises(httpx.HTTPStatusError, match="create PR.*422"):
            client.create_pr("t", "b", "branch", "main")


class TestAddLabels:
    @respx.mock
    def test_adds_labels(self, client):
        route = respx.post(f"{BASE_URL}/issues/10/labels").mock(
            return_value=httpx.Response(200, json=[])
        )

        client.add_labels(10, ["wowkmang/done", "enhancement"])

        import json

        payload = json.loads(route.calls[0].request.read())
        assert payload == {"labels": ["wowkmang/done", "enhancement"]}

    @respx.mock
    def test_error_raises(self, client):
        respx.post(f"{BASE_URL}/issues/10/labels").mock(
            return_value=httpx.Response(403, json={"message": "Forbidden"})
        )

        with pytest.raises(httpx.HTTPStatusError, match="add labels.*403"):
            client.add_labels(10, ["label"])


class TestRemoveLabel:
    @respx.mock
    def test_removes_label(self, client):
        route = respx.delete(f"{BASE_URL}/issues/10/labels/wowkmang").mock(
            return_value=httpx.Response(200, json=[])
        )

        client.remove_label(10, "wowkmang")

        assert route.called

    @respx.mock
    def test_404_is_ignored(self, client):
        respx.delete(f"{BASE_URL}/issues/10/labels/gone").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )

        # Should not raise
        client.remove_label(10, "gone")

    @respx.mock
    def test_other_error_raises(self, client):
        respx.delete(f"{BASE_URL}/issues/10/labels/x").mock(
            return_value=httpx.Response(500, json={"message": "Server Error"})
        )

        with pytest.raises(httpx.HTTPStatusError, match="remove label.*500"):
            client.remove_label(10, "x")


class TestGetIssueComments:
    @respx.mock
    def test_fetches_comments(self, client):
        respx.get(f"{BASE_URL}/issues/10/comments").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"user": {"login": "alice"}, "body": "Looks good"},
                    {"user": {"login": "bob"}, "body": "Needs work"},
                ],
            )
        )

        comments = client.get_issue_comments(10)

        assert len(comments) == 2
        assert comments[0] == {"user": "alice", "body": "Looks good"}
        assert comments[1] == {"user": "bob", "body": "Needs work"}

    @respx.mock
    def test_empty_comments(self, client):
        respx.get(f"{BASE_URL}/issues/5/comments").mock(
            return_value=httpx.Response(200, json=[])
        )

        assert client.get_issue_comments(5) == []

    @respx.mock
    def test_error_raises(self, client):
        respx.get(f"{BASE_URL}/issues/10/comments").mock(
            return_value=httpx.Response(403, json={"message": "Forbidden"})
        )

        with pytest.raises(httpx.HTTPStatusError, match="get issue comments.*403"):
            client.get_issue_comments(10)


class TestGetPRComments:
    @respx.mock
    def test_fetches_both_types(self, client):
        respx.get(f"{BASE_URL}/issues/7/comments").mock(
            return_value=httpx.Response(
                200,
                json=[{"user": {"login": "alice"}, "body": "LGTM"}],
            )
        )
        respx.get(f"{BASE_URL}/pulls/7/comments").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "user": {"login": "bob"},
                        "body": "Fix this",
                        "path": "src/main.py",
                    },
                ],
            )
        )

        comments = client.get_pr_comments(7)

        assert len(comments) == 2
        assert comments[0] == {"user": "alice", "body": "LGTM"}
        assert comments[1] == {"user": "bob", "body": "Fix this", "path": "src/main.py"}


class TestGetPRBranch:
    @respx.mock
    def test_returns_head_ref(self, client):
        respx.get(f"{BASE_URL}/pulls/7").mock(
            return_value=httpx.Response(
                200,
                json={"head": {"ref": "feature/my-branch"}},
            )
        )

        assert client.get_pr_branch(7) == "feature/my-branch"

    @respx.mock
    def test_error_raises(self, client):
        respx.get(f"{BASE_URL}/pulls/999").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )

        with pytest.raises(httpx.HTTPStatusError, match="get PR branch.*404"):
            client.get_pr_branch(999)


class TestCreateComment:
    @respx.mock
    def test_posts_comment(self, client):
        import json

        route = respx.post(f"{BASE_URL}/issues/42/comments").mock(
            return_value=httpx.Response(201, json={"id": 1, "body": "Hello"})
        )

        result = client.create_comment(42, "Hello")

        assert route.called
        payload = json.loads(route.calls[0].request.read())
        assert payload == {"body": "Hello"}
        assert result["body"] == "Hello"

    @respx.mock
    def test_error_raises(self, client):
        respx.post(f"{BASE_URL}/issues/42/comments").mock(
            return_value=httpx.Response(403, json={"message": "Forbidden"})
        )

        with pytest.raises(httpx.HTTPStatusError, match="create comment.*403"):
            client.create_comment(42, "test")


class TestFetchAndSaveComments:
    @respx.mock
    def test_saves_issue_comments(self, tmp_path):
        respx.get(f"{BASE_URL}/issues/10/comments").mock(
            return_value=httpx.Response(
                200,
                json=[{"user": {"login": "alice"}, "body": "Hello"}],
            )
        )

        context_dir = tmp_path / "context"
        result = fetch_and_save_comments(
            github_token="ghp_test123",
            repo_full_name="user/project",
            source_type="github_issue",
            source_number=10,
            task_id="abc123",
            context_dir=context_dir,
        )

        assert result is not None
        assert context_dir.exists()
        data = json.loads((context_dir / "abc123_comments.json").read_text())
        assert len(data) == 1
        assert data[0]["user"] == "alice"

    @respx.mock
    def test_saves_pr_comments(self, tmp_path):
        respx.get(f"{BASE_URL}/issues/7/comments").mock(
            return_value=httpx.Response(200, json=[])
        )
        respx.get(f"{BASE_URL}/pulls/7/comments").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"user": {"login": "bob"}, "body": "Fix", "path": "a.py"},
                ],
            )
        )

        context_dir = tmp_path / "context"
        result = fetch_and_save_comments(
            github_token="ghp_test123",
            repo_full_name="user/project",
            source_type="github_pr",
            source_number=7,
            task_id="def456",
            context_dir=context_dir,
        )

        assert result is not None
        data = json.loads((context_dir / "def456_comments.json").read_text())
        assert len(data) == 1

    @respx.mock
    def test_returns_none_when_no_comments(self, tmp_path):
        respx.get(f"{BASE_URL}/issues/5/comments").mock(
            return_value=httpx.Response(200, json=[])
        )

        result = fetch_and_save_comments(
            github_token="ghp_test123",
            repo_full_name="user/project",
            source_type="github_issue",
            source_number=5,
            task_id="ghi789",
            context_dir=tmp_path / "context",
        )

        assert result is None

    def test_returns_none_on_error(self, tmp_path):
        # No mock — will fail to connect
        result = fetch_and_save_comments(
            github_token="ghp_test123",
            repo_full_name="user/project",
            source_type="github_issue",
            source_number=10,
            task_id="err",
            context_dir=tmp_path / "context",
        )

        assert result is None


class TestHeaders:
    @respx.mock
    def test_auth_header_is_set(self, client):
        route = respx.post(f"{BASE_URL}/pulls").mock(
            return_value=httpx.Response(201, json={"number": 1})
        )

        client.create_pr("t", "b", "h", "main")

        request = route.calls[0].request
        assert request.headers["authorization"] == "Bearer ghp_test123"
        assert "application/vnd.github+json" in request.headers["accept"]

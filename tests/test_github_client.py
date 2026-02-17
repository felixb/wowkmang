import httpx
import pytest
import respx

from wowkmang.github_client import GitHubClient

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

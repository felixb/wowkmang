import json
from unittest.mock import MagicMock, patch

import pytest
from github import GithubException

from wowkmang.executor.github_client import GitHubClient, fetch_and_save_comments


@pytest.fixture
def mock_repo():
    return MagicMock()


@pytest.fixture
def client(mock_repo):
    with patch("wowkmang.executor.github_client.Github") as MockGithub:
        MockGithub.return_value.get_repo.return_value = mock_repo
        yield GitHubClient(token="ghp_test123", repo="user/project")


class TestCreatePR:
    def test_creates_pr_with_correct_payload(self, client, mock_repo):
        mock_pr = MagicMock()
        mock_pr.number = 42
        mock_pr.html_url = "https://github.com/user/project/pull/42"
        mock_repo.create_pull.return_value = mock_pr

        result = client.create_pr(
            title="Fix login bug",
            body="Closes #10",
            branch="wowkmang/fix-login",
            base="main",
        )

        mock_repo.create_pull.assert_called_once_with(
            title="Fix login bug",
            body="Closes #10",
            head="wowkmang/fix-login",
            base="main",
            draft=False,
        )
        assert result["number"] == 42

    def test_creates_draft_pr(self, client, mock_repo):
        mock_pr = MagicMock()
        mock_pr.number = 43
        mock_repo.create_pull.return_value = mock_pr

        result = client.create_pr(
            title="WIP: fix",
            body="Draft",
            branch="wowkmang/wip",
            base="main",
            draft=True,
        )

        call_kwargs = mock_repo.create_pull.call_args.kwargs
        assert call_kwargs["draft"] is True
        assert result["number"] == 43

    def test_http_error_raises(self, client, mock_repo):
        mock_repo.create_pull.side_effect = GithubException(
            422, {"message": "Validation Failed"}
        )

        with pytest.raises(GithubException) as exc_info:
            client.create_pr("t", "b", "branch", "main")

        assert exc_info.value.status == 422


class TestAddLabels:
    def test_adds_labels(self, client, mock_repo):
        mock_issue = MagicMock()
        mock_repo.get_issue.return_value = mock_issue

        client.add_labels(10, ["wowkmang/done", "enhancement"])

        mock_repo.get_issue.assert_called_once_with(10)
        mock_issue.add_to_labels.assert_called_once_with("wowkmang/done", "enhancement")

    def test_error_raises(self, client, mock_repo):
        mock_issue = MagicMock()
        mock_repo.get_issue.return_value = mock_issue
        mock_issue.add_to_labels.side_effect = GithubException(
            403, {"message": "Forbidden"}
        )

        with pytest.raises(GithubException) as exc_info:
            client.add_labels(10, ["label"])

        assert exc_info.value.status == 403


class TestRemoveLabel:
    def test_removes_label(self, client, mock_repo):
        mock_issue = MagicMock()
        mock_repo.get_issue.return_value = mock_issue

        client.remove_label(10, "wowkmang")

        mock_repo.get_issue.assert_called_once_with(10)
        mock_issue.remove_from_labels.assert_called_once_with("wowkmang")

    def test_404_is_ignored(self, client, mock_repo):
        mock_issue = MagicMock()
        mock_repo.get_issue.return_value = mock_issue
        mock_issue.remove_from_labels.side_effect = GithubException(
            404, {"message": "Not Found"}
        )

        # Should not raise
        client.remove_label(10, "gone")

    def test_other_error_raises(self, client, mock_repo):
        mock_issue = MagicMock()
        mock_repo.get_issue.return_value = mock_issue
        mock_issue.remove_from_labels.side_effect = GithubException(
            500, {"message": "Server Error"}
        )

        with pytest.raises(GithubException) as exc_info:
            client.remove_label(10, "x")

        assert exc_info.value.status == 500


class TestGetIssueComments:
    def test_fetches_comments(self, client, mock_repo):
        mock_issue = MagicMock()
        mock_repo.get_issue.return_value = mock_issue

        comment1 = MagicMock()
        comment1.user.login = "alice"
        comment1.body = "Looks good"
        comment2 = MagicMock()
        comment2.user.login = "bob"
        comment2.body = "Needs work"
        mock_issue.get_comments.return_value = [comment1, comment2]

        comments = client.get_issue_comments(10)

        assert len(comments) == 2
        assert comments[0] == {"user": "alice", "body": "Looks good"}
        assert comments[1] == {"user": "bob", "body": "Needs work"}

    def test_empty_comments(self, client, mock_repo):
        mock_issue = MagicMock()
        mock_repo.get_issue.return_value = mock_issue
        mock_issue.get_comments.return_value = []

        assert client.get_issue_comments(5) == []

    def test_error_raises(self, client, mock_repo):
        mock_issue = MagicMock()
        mock_repo.get_issue.return_value = mock_issue
        mock_issue.get_comments.side_effect = GithubException(
            403, {"message": "Forbidden"}
        )

        with pytest.raises(GithubException) as exc_info:
            client.get_issue_comments(10)

        assert exc_info.value.status == 403


class TestGetPRComments:
    def test_fetches_both_types(self, client, mock_repo):
        mock_issue = MagicMock()
        mock_pr = MagicMock()
        mock_repo.get_issue.return_value = mock_issue
        mock_repo.get_pull.return_value = mock_pr

        issue_comment = MagicMock()
        issue_comment.user.login = "alice"
        issue_comment.body = "LGTM"
        mock_issue.get_comments.return_value = [issue_comment]

        review_comment = MagicMock()
        review_comment.user.login = "bob"
        review_comment.body = "Fix this"
        review_comment.path = "src/main.py"
        mock_pr.get_review_comments.return_value = [review_comment]

        comments = client.get_pr_comments(7)

        assert len(comments) == 2
        assert comments[0] == {"user": "alice", "body": "LGTM"}
        assert comments[1] == {"user": "bob", "body": "Fix this", "path": "src/main.py"}


class TestGetPRBranch:
    def test_returns_head_ref(self, client, mock_repo):
        mock_pr = MagicMock()
        mock_repo.get_pull.return_value = mock_pr
        mock_pr.head.ref = "feature/my-branch"

        assert client.get_pr_branch(7) == "feature/my-branch"

    def test_error_raises(self, client, mock_repo):
        mock_repo.get_pull.side_effect = GithubException(404, {"message": "Not Found"})

        with pytest.raises(GithubException) as exc_info:
            client.get_pr_branch(999)

        assert exc_info.value.status == 404


class TestCreateComment:
    def test_posts_comment(self, client, mock_repo):
        mock_issue = MagicMock()
        mock_repo.get_issue.return_value = mock_issue

        mock_comment = MagicMock()
        mock_comment.id = 1
        mock_comment.body = "Hello"
        mock_issue.create_comment.return_value = mock_comment

        result = client.create_comment(42, "Hello")

        mock_issue.create_comment.assert_called_once_with("Hello")
        assert result["body"] == "Hello"

    def test_error_raises(self, client, mock_repo):
        mock_issue = MagicMock()
        mock_repo.get_issue.return_value = mock_issue
        mock_issue.create_comment.side_effect = GithubException(
            403, {"message": "Forbidden"}
        )

        with pytest.raises(GithubException) as exc_info:
            client.create_comment(42, "test")

        assert exc_info.value.status == 403


class TestFetchAndSaveComments:
    def test_saves_issue_comments(self, tmp_path):
        with patch("wowkmang.executor.github_client.Github") as MockGithub:
            mock_repo = MagicMock()
            MockGithub.return_value.get_repo.return_value = mock_repo

            mock_issue = MagicMock()
            mock_repo.get_issue.return_value = mock_issue

            comment = MagicMock()
            comment.user.login = "alice"
            comment.body = "Hello"
            mock_issue.get_comments.return_value = [comment]

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

    def test_saves_pr_comments(self, tmp_path):
        with patch("wowkmang.executor.github_client.Github") as MockGithub:
            mock_repo = MagicMock()
            MockGithub.return_value.get_repo.return_value = mock_repo

            mock_issue = MagicMock()
            mock_pr = MagicMock()
            mock_repo.get_issue.return_value = mock_issue
            mock_repo.get_pull.return_value = mock_pr

            mock_issue.get_comments.return_value = []

            review_comment = MagicMock()
            review_comment.user.login = "bob"
            review_comment.body = "Fix"
            review_comment.path = "a.py"
            mock_pr.get_review_comments.return_value = [review_comment]

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

    def test_returns_none_when_no_comments(self, tmp_path):
        with patch("wowkmang.executor.github_client.Github") as MockGithub:
            mock_repo = MagicMock()
            MockGithub.return_value.get_repo.return_value = mock_repo

            mock_issue = MagicMock()
            mock_repo.get_issue.return_value = mock_issue
            mock_issue.get_comments.return_value = []

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
        with patch("wowkmang.executor.github_client.Github") as MockGithub:
            MockGithub.return_value.get_repo.side_effect = GithubException(
                401, {"message": "Unauthorized"}
            )

            result = fetch_and_save_comments(
                github_token="ghp_test123",
                repo_full_name="user/project",
                source_type="github_issue",
                source_number=10,
                task_id="err",
                context_dir=tmp_path / "context",
            )

            assert result is None

    def test_filters_comments_by_allowed_users(self, tmp_path):
        with patch("wowkmang.executor.github_client.Github") as MockGithub:
            mock_repo = MagicMock()
            MockGithub.return_value.get_repo.return_value = mock_repo

            mock_issue = MagicMock()
            mock_repo.get_issue.return_value = mock_issue

            alice = MagicMock()
            alice.user.login = "alice"
            alice.body = "Allowed comment"
            bob = MagicMock()
            bob.user.login = "bob"
            bob.body = "Disallowed comment"
            mock_issue.get_comments.return_value = [alice, bob]

            context_dir = tmp_path / "context"
            result = fetch_and_save_comments(
                github_token="ghp_test123",
                repo_full_name="user/project",
                source_type="github_issue",
                source_number=10,
                task_id="filtered",
                context_dir=context_dir,
                allowed_users=["alice"],
            )

            assert result is not None
            data = json.loads((context_dir / "filtered_comments.json").read_text())
            assert len(data) == 1
            assert data[0]["user"] == "alice"

    def test_allowed_users_empty_returns_none_when_all_filtered(self, tmp_path):
        """When allowed_users filters out all comments, returns None."""
        with patch("wowkmang.executor.github_client.Github") as MockGithub:
            mock_repo = MagicMock()
            MockGithub.return_value.get_repo.return_value = mock_repo

            mock_issue = MagicMock()
            mock_repo.get_issue.return_value = mock_issue

            stranger = MagicMock()
            stranger.user.login = "stranger"
            stranger.body = "Random comment"
            mock_issue.get_comments.return_value = [stranger]

            result = fetch_and_save_comments(
                github_token="ghp_test123",
                repo_full_name="user/project",
                source_type="github_issue",
                source_number=10,
                task_id="allfiltered",
                context_dir=tmp_path / "context",
                allowed_users=["alice", "bob"],
            )

            assert result is None

    def test_no_allowed_users_includes_all_comments(self, tmp_path):
        """Without allowed_users filter, all comments are included."""
        with patch("wowkmang.executor.github_client.Github") as MockGithub:
            mock_repo = MagicMock()
            MockGithub.return_value.get_repo.return_value = mock_repo

            mock_issue = MagicMock()
            mock_repo.get_issue.return_value = mock_issue

            alice = MagicMock()
            alice.user.login = "alice"
            alice.body = "Comment A"
            bob = MagicMock()
            bob.user.login = "bob"
            bob.body = "Comment B"
            mock_issue.get_comments.return_value = [alice, bob]

            context_dir = tmp_path / "context"
            result = fetch_and_save_comments(
                github_token="ghp_test123",
                repo_full_name="user/project",
                source_type="github_issue",
                source_number=10,
                task_id="nofilter",
                context_dir=context_dir,
            )

            assert result is not None
            data = json.loads((context_dir / "nofilter_comments.json").read_text())
            assert len(data) == 2

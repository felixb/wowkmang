import json
import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


def fetch_and_save_comments(
    github_token: str,
    repo_full_name: str,
    source_type: str,
    source_number: int,
    task_id: str,
    context_dir: Path,
) -> str | None:
    """Fetch comments from GitHub and save to a context file. Returns file path."""
    try:
        gh = GitHubClient(token=github_token, repo=repo_full_name)
        if source_type == "github_pr":
            comments = gh.get_pr_comments(source_number)
        else:
            comments = gh.get_issue_comments(source_number)

        if not comments:
            return None

        context_dir.mkdir(exist_ok=True)
        filepath = context_dir / f"{task_id}_comments.json"
        filepath.write_text(json.dumps(comments))
        return str(filepath)
    except Exception:
        logger.warning("Could not fetch comments for #%s", source_number)
        return None


class GitHubClient:
    def __init__(self, token: str, repo: str):
        self.repo = repo  # "user/project"
        self._client = httpx.Client(
            base_url=f"https://api.github.com/repos/{repo}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )

    def create_pr(
        self,
        title: str,
        body: str,
        branch: str,
        base: str,
        draft: bool = False,
    ) -> dict:
        """Create a pull request via GitHub API."""
        response = self._client.post(
            "/pulls",
            json={
                "title": title,
                "body": body,
                "head": branch,
                "base": base,
                "draft": draft,
            },
        )
        self._raise_for_status(response, "create PR")
        return response.json()

    def add_labels(self, issue_number: int, labels: list[str]) -> None:
        """Add labels to an issue or PR."""
        response = self._client.post(
            f"/issues/{issue_number}/labels",
            json={"labels": labels},
        )
        self._raise_for_status(response, "add labels")

    def get_issue_comments(self, issue_number: int) -> list[dict]:
        """Fetch comments on an issue."""
        response = self._client.get(
            f"/issues/{issue_number}/comments",
            params={"per_page": 100},
        )
        self._raise_for_status(response, "get issue comments")
        return [
            {"user": c["user"]["login"], "body": c["body"]} for c in response.json()
        ]

    def get_pr_comments(self, pr_number: int) -> list[dict]:
        """Fetch both issue comments and review comments on a PR."""
        # Issue-style comments
        issue_resp = self._client.get(
            f"/issues/{pr_number}/comments",
            params={"per_page": 100},
        )
        self._raise_for_status(issue_resp, "get PR issue comments")

        # Review comments (inline)
        review_resp = self._client.get(
            f"/pulls/{pr_number}/comments",
            params={"per_page": 100},
        )
        self._raise_for_status(review_resp, "get PR review comments")

        comments = []
        for c in issue_resp.json():
            comments.append({"user": c["user"]["login"], "body": c["body"]})
        for c in review_resp.json():
            comments.append(
                {
                    "user": c["user"]["login"],
                    "body": c["body"],
                    "path": c.get("path"),
                }
            )
        return comments

    def get_pr_branch(self, pr_number: int) -> str:
        """Get the head branch name of a PR."""
        response = self._client.get(f"/pulls/{pr_number}")
        self._raise_for_status(response, "get PR branch")
        return response.json()["head"]["ref"]

    def create_comment(self, issue_number: int, body: str) -> dict:
        """Post a comment on an issue or PR."""
        response = self._client.post(
            f"/issues/{issue_number}/comments",
            json={"body": body},
        )
        self._raise_for_status(response, "create comment")
        return response.json()

    def remove_label(self, issue_number: int, label: str) -> None:
        """Remove a label from an issue or PR."""
        response = self._client.delete(
            f"/issues/{issue_number}/labels/{label}",
        )
        # 404 is fine — label might already be removed
        if response.status_code != 404:
            self._raise_for_status(response, "remove label")

    @staticmethod
    def _raise_for_status(response: httpx.Response, action: str) -> None:
        """Raise on non-2xx with a useful error message."""
        if response.is_success:
            return
        raise httpx.HTTPStatusError(
            f"GitHub API error on {action}: {response.status_code} {response.text}",
            request=response.request,
            response=response,
        )

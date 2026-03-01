import json
import logging
from pathlib import Path

from github import Auth, Github, GithubException

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
        self._repo = Github(auth=Auth.Token(token)).get_repo(repo)

    def create_pr(
        self,
        title: str,
        body: str,
        branch: str,
        base: str,
        draft: bool = False,
    ) -> dict:
        """Create a pull request via GitHub API."""
        pr = self._repo.create_pull(
            title=title,
            body=body,
            head=branch,
            base=base,
            draft=draft,
        )
        return {"number": pr.number, "html_url": pr.html_url}

    def add_labels(self, issue_number: int, labels: list[str]) -> None:
        """Add labels to an issue or PR."""
        self._repo.get_issue(issue_number).add_to_labels(*labels)

    def get_issue_comments(self, issue_number: int) -> list[dict]:
        """Fetch comments on an issue."""
        return [
            {"user": c.user.login, "body": c.body}
            for c in self._repo.get_issue(issue_number).get_comments()
        ]

    def get_pr_comments(self, pr_number: int) -> list[dict]:
        """Fetch both issue comments and review comments on a PR."""
        comments = []
        for c in self._repo.get_issue(pr_number).get_comments():
            comments.append({"user": c.user.login, "body": c.body})
        for c in self._repo.get_pull(pr_number).get_review_comments():
            comments.append(
                {
                    "user": c.user.login,
                    "body": c.body,
                    "path": c.path,
                }
            )
        return comments

    def get_pr_branch(self, pr_number: int) -> str:
        """Get the head branch name of a PR."""
        return self._repo.get_pull(pr_number).head.ref

    def create_comment(self, issue_number: int, body: str) -> dict:
        """Post a comment on an issue or PR."""
        comment = self._repo.get_issue(issue_number).create_comment(body)
        return {"id": comment.id, "body": comment.body}

    def remove_label(self, issue_number: int, label: str) -> None:
        """Remove a label from an issue or PR."""
        try:
            self._repo.get_issue(issue_number).remove_from_labels(label)
        except GithubException as e:
            # 404 is fine — label might already be removed
            if e.status != 404:
                raise

import httpx


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

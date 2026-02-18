import uuid
from urllib.parse import urlparse, urlunparse

from wowkmang.docker_runner import DockerRunner


class RepoCache:
    def __init__(self, docker_runner: DockerRunner):
        self.docker_runner = docker_runner

    def prepare_workdir(
        self,
        repo_url: str,
        ref: str,
        work_volume: str,
        image: str,
        github_token: str | None = None,
    ) -> str:
        """Clone repo into work volume using cache for speed. Returns branch name."""
        authed_url = self._authed_url(repo_url, github_token)
        cache_subdir = self.cache_subdir(repo_url)
        branch_name = f"wowkmang/{uuid.uuid4().hex[:8]}"

        script = (
            f"set -e\n"
            f"if [ -d /cache/{cache_subdir} ]; then\n"
            f"  git -C /cache/{cache_subdir} fetch --all\n"
            f"else\n"
            f"  mkdir -p /cache\n"
            f"  git clone --bare {authed_url} /cache/{cache_subdir}\n"
            f"fi\n"
            f"cp -r /cache/{cache_subdir} /workspace/.repo-cache\n"
            f"git clone --reference /workspace/.repo-cache {authed_url} /workspace/repo\n"
            f"cd /workspace/repo\n"
            f"git checkout -b {branch_name} origin/{ref}\n"
        )

        environment = {"GIT_TERMINAL_PROMPT": "0"}
        result = self.docker_runner.run_command(
            work_dir=work_volume,
            command=["sh", "-c", script],
            image=image,
            environment=environment,
            timeout_seconds=300,
        )

        if result.exit_code != 0:
            raise RuntimeError(
                f"Git preparation failed (exit {result.exit_code}):\n{result.logs}"
            )

        return branch_name

    @staticmethod
    def _authed_url(repo_url: str, token: str | None) -> str:
        """Inject token into HTTPS URL for authentication."""
        if not token:
            return repo_url
        parsed = urlparse(repo_url)
        host = parsed.hostname or parsed.netloc
        authed = parsed._replace(netloc=f"x-access-token:{token}@{host}")
        return urlunparse(authed)

    @staticmethod
    def cache_subdir(repo_url: str) -> str:
        """Convert repo URL to cache subdirectory name."""
        cleaned = repo_url.replace("https://", "")
        if cleaned.endswith(".git"):
            cleaned = cleaned[:-4]
        sanitized = cleaned.replace("/", "_")
        return sanitized

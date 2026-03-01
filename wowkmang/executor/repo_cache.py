import re
import shlex
import uuid

from wowkmang.executor.docker_runner import DockerRunner

SAFE_BRANCH_RE = re.compile(r"^[\w./-]+$")


class RepoCache:
    def __init__(self, docker_runner: DockerRunner):
        self.docker_runner = docker_runner

    def prepare_workdir(
        self,
        repo_url: str,
        ref: str,
        work_volume: str,
        project_volume: str,
        image: str,
        github_token: str | None = None,
        existing_branch: str | None = None,
    ) -> str:
        """Clone repo into work volume using project cache for speed. Returns branch name.

        Authentication is handled via .netrc on the project volume (HOME=/cache),
        so tokens never appear in command arguments or logs.
        """
        cache_subdir = self.cache_subdir(repo_url)

        if existing_branch:
            if not SAFE_BRANCH_RE.match(existing_branch):
                raise ValueError(f"Invalid branch name: {existing_branch!r}")
            branch_name = existing_branch
            checkout_cmd = f"git checkout {shlex.quote(existing_branch)}\n"
        else:
            branch_name = f"wowkmang/{uuid.uuid4().hex[:8]}"
            checkout_cmd = f"git checkout -b {shlex.quote(branch_name)} origin/{shlex.quote(ref)}\n"

        script = (
            f"set -e\n"
            f"if [ -d /cache/{cache_subdir}/.git ]; then\n"
            f"  git -C /cache/{cache_subdir} fetch --all\n"
            f"else\n"
            f"  rm -rf /cache/{cache_subdir}\n"
            f"  mkdir -p /cache\n"
            f"  git clone --bare {repo_url} /cache/{cache_subdir}\n"
            f"fi\n"
            f"git clone --reference /cache/{cache_subdir} {repo_url} /workspace/repo\n"
            f"cd /workspace/repo\n" + checkout_cmd
        )

        environment = {"GIT_TERMINAL_PROMPT": "0"}
        result = self.docker_runner.run_command(
            work_dir=work_volume,
            project_volume=project_volume,
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
    def cache_subdir(repo_url: str) -> str:
        """Convert repo URL to cache subdirectory name."""
        cleaned = repo_url.replace("https://", "")
        if cleaned.endswith(".git"):
            cleaned = cleaned[:-4]
        sanitized = cleaned.replace("/", "_")
        return sanitized

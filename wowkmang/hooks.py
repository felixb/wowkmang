from enum import Enum

from pydantic import BaseModel

from wowkmang.config import ProjectConfig
from wowkmang.docker_runner import DockerRunner


class HookResult(BaseModel):
    success: bool
    output: str
    exit_code: int


class HookType(Enum):
    PRE = "pre"
    POST = "post"


class HookRunner:
    def __init__(self, docker_runner: DockerRunner):
        self.docker_runner = docker_runner
        self._has_pre_commit_config: dict[str, bool] = {}

    def _check_pre_commit_config(
        self, work_dir: str, project_volume: str, project: ProjectConfig
    ) -> bool:
        if work_dir in self._has_pre_commit_config:
            return self._has_pre_commit_config[work_dir]

        result = self.docker_runner.run_command(
            work_dir=work_dir,
            project_volume=project_volume,
            command="test -f .pre-commit-config.yaml",
            image=project.docker_image,
            environment={
                "GITHUB_TOKEN": project.github_token or self.docker_runner.github_token
            },
            timeout_seconds=5,
        )
        has_config = result.exit_code == 0
        self._has_pre_commit_config[work_dir] = has_config
        return has_config

    def has_pre_commit(
        self, work_dir: str, project_volume: str, project: ProjectConfig
    ) -> bool:
        """Return True if the repo has a .pre-commit-config.yaml."""
        return self._check_pre_commit_config(work_dir, project_volume, project)

    def run_pre_commit(
        self, work_dir: str, project_volume: str, project: ProjectConfig
    ) -> HookResult:
        """Run pre-commit run -a once and return the result."""
        result = self.docker_runner.run_hooks(
            work_dir, project_volume, ["pre-commit run -a"], project
        )
        return HookResult(
            success=result.exit_code == 0,
            output=result.logs,
            exit_code=result.exit_code,
        )

    def stage_changes(
        self, work_dir: str, project_volume: str, project: ProjectConfig
    ) -> None:
        """Stage all changes (including hook auto-fixes) for the next commit."""
        self.docker_runner.run_command(
            work_dir=work_dir,
            project_volume=project_volume,
            command=["git", "add", "-A"],
            image=project.docker_image,
        )

    def run_hooks(
        self,
        hook_type: HookType,
        work_dir: str,
        project_volume: str,
        project: ProjectConfig,
    ) -> HookResult:
        """Run pre- or post-task hook commands. Returns result with success/failure."""
        if hook_type == HookType.PRE:
            commands = project.pre_task
        elif hook_type == HookType.POST:
            commands = project.post_task
        else:
            raise NotImplementedError(f"Unknown hook type: {hook_type}")
        if not commands:
            return HookResult(success=True, output="", exit_code=0)
        result = self.docker_runner.run_hooks(
            work_dir, project_volume, commands, project
        )
        return HookResult(
            success=result.exit_code == 0,
            output=result.logs,
            exit_code=result.exit_code,
        )

    def run_post_task_checks(
        self, work_dir: str, project_volume: str, project: ProjectConfig
    ) -> HookResult:
        """Run pre-commit (auto-fix → stage → verify) then post hooks."""
        if self.has_pre_commit(work_dir, project_volume, project):
            self.run_pre_commit(work_dir, project_volume, project)
            self.stage_changes(work_dir, project_volume, project)
            verify = self.run_pre_commit(work_dir, project_volume, project)
            if not verify.success:
                return verify

        return self.run_hooks(HookType.POST, work_dir, project_volume, project)

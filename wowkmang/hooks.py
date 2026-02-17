from pydantic import BaseModel

from wowkmang.config import ProjectConfig
from wowkmang.docker_runner import DockerRunner
from wowkmang.models import Task


class HookResult(BaseModel):
    success: bool
    output: str
    exit_code: int


class HookRunner:
    def __init__(self, docker_runner: DockerRunner):
        self.docker_runner = docker_runner

    def run_hooks(
        self, commands: list[str], work_dir: str, project: ProjectConfig
    ) -> HookResult:
        """Run hook commands in a container. Returns result with success/failure."""
        result = self.docker_runner.run_hooks(work_dir, commands, project)
        return HookResult(
            success=result.exit_code == 0,
            output=result.logs,
            exit_code=result.exit_code,
        )


class FixLoop:
    def __init__(self, docker_runner: DockerRunner, hook_runner: HookRunner):
        self.docker_runner = docker_runner
        self.hook_runner = hook_runner

    def run(
        self,
        task: Task,
        project: ProjectConfig,
        work_dir: str,
        hook_failure: HookResult,
    ) -> HookResult:
        """Attempt to fix post-task hook failures. Returns final hook result."""
        last_result = hook_failure

        for attempt in range(project.max_fix_attempts):
            fix_prompt = (
                "The following checks failed after your changes. "
                f"Fix the issues:\n\n{last_result.output}"
            )

            model = task.model or project.default_model
            self.docker_runner.run_claude_code(
                work_dir=work_dir,
                task_prompt=fix_prompt,
                model=model,
                project=project,
                timeout_minutes=project.timeout_minutes,
            )

            last_result = self.hook_runner.run_hooks(
                project.post_task, work_dir, project
            )

            if last_result.success:
                return last_result

        return last_result

import logging
import shlex
import threading
import time
from datetime import datetime, timezone
from enum import Enum

from wowkmang.config import GlobalConfig, ProjectConfig
from wowkmang.docker_runner import ContainerResult, DockerRunner
from wowkmang.github_client import GitHubClient
from wowkmang.hooks import FixLoop, HookRunner
from wowkmang.models import Task, TaskResult, TaskStatus, task_from_yaml, task_to_yaml
from wowkmang.queue import complete_task, fail_task, pick_next_task
from wowkmang.repo_cache import RepoCache
from wowkmang.summary import SummaryGenerator

logger = logging.getLogger(__name__)


class WorkerStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"


class Worker:
    def __init__(
        self,
        config: GlobalConfig,
        projects: dict[str, ProjectConfig],
        docker_runner: DockerRunner,
        repo_cache: RepoCache,
        hook_runner: HookRunner,
        fix_loop: FixLoop,
        summary_generator: SummaryGenerator,
    ):
        self.config = config
        self.projects = projects
        self.docker_runner = docker_runner
        self.repo_cache = repo_cache
        self.hook_runner = hook_runner
        self.fix_loop = fix_loop
        self.summary_generator = summary_generator
        self._status = WorkerStatus.IDLE
        self._running = False
        self._thread: threading.Thread | None = None

    @property
    def status(self) -> str:
        return self._status.value

    def start(self) -> None:
        self._recover_stale_tasks()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        while self._running:
            result = pick_next_task(self.config.tasks_dir)
            if result:
                task_file, task = result
                self._status = WorkerStatus.RUNNING
                try:
                    self._process_task(task_file, task)
                except Exception as e:
                    logger.exception("Unhandled error processing task %s", task.id)
                    task.result = TaskResult(
                        status=TaskStatus.FAILED,
                        error=f"Internal error: {e}",
                        finished=datetime.now(timezone.utc),
                    )
                    fail_task(self.config.tasks_dir, task_file, task)
                finally:
                    self._status = WorkerStatus.IDLE
            else:
                time.sleep(5)

    def _process_task(self, task_file, task: Task) -> None:
        project = self.projects.get(task.project)
        if not project:
            task.result = TaskResult(
                status=TaskStatus.FAILED,
                error=f"Unknown project: {task.project}",
                finished=datetime.now(timezone.utc),
            )
            fail_task(self.config.tasks_dir, task_file, task)
            return

        start_time = datetime.now(timezone.utc)
        work_volume = self.docker_runner.create_volume(prefix="wowkmang-work")
        session_volume = self.docker_runner.create_volume(prefix="wowkmang-session")
        self._seed_claude_config(session_volume, project.docker_image)

        try:
            self._run_task_pipeline(
                task_file, task, project, work_volume, session_volume, start_time
            )
        except Exception as e:
            logger.exception("Unhandled error processing task %s", task.id)
            task.result = TaskResult(
                status=TaskStatus.FAILED,
                error=f"Internal error: {e}",
                finished=datetime.now(timezone.utc),
            )
            fail_task(self.config.tasks_dir, task_file, task)
        finally:
            self.docker_runner.remove_volume(session_volume)
            if self.config.keep_workdir:
                logger.info("Keeping work volume for inspection: %s", work_volume)
            else:
                self.docker_runner.remove_volume(work_volume)

    def _run_task_pipeline(
        self,
        task_file,
        task: Task,
        project: ProjectConfig,
        work_volume: str,
        session_volume: str,
        start_time: datetime,
    ) -> None:
        github_token = project.credentials.get("github_token")
        image = project.docker_image

        # Pull image once for the entire task
        self.docker_runner.ensure_image(image, project)

        # Create .wowkmang dir for step logs
        self._create_wowkmang_dir(work_volume, image)

        # Repo preparation
        branch = self.repo_cache.prepare_workdir(
            task.repo, task.ref, work_volume, image, github_token
        )

        # Copy debug artifacts into workdir
        cache_subdir = RepoCache.cache_subdir(task.repo)
        copy_result = self.docker_runner.copy_to_workdir(
            work_volume=work_volume,
            session_volume=session_volume,
            cache_subdir=cache_subdir,
            image=image,
        )
        self._log_step("copy_to_workdir", copy_result, work_volume, image)

        # Pre-task hooks
        if project.pre_task:
            pre_result = self.hook_runner.run_hooks(
                project.pre_task, work_volume, project
            )
            if not pre_result.success:
                duration = int(
                    (datetime.now(timezone.utc) - start_time).total_seconds()
                )
                task.result = TaskResult(
                    status=TaskStatus.FAILED,
                    error=f"Pre-task hooks failed:\n{pre_result.output}",
                    duration_seconds=duration,
                    finished=datetime.now(timezone.utc),
                )
                fail_task(self.config.tasks_dir, task_file, task)
                return

        # Claude Code execution
        model = task.model or project.default_model
        cc_result = self.docker_runner.run_claude_code(
            work_dir=work_volume,
            task_prompt=task.task,
            model=model,
            project=project,
            timeout_minutes=project.timeout_minutes,
            session_dir=session_volume,
        )
        self._log_step("claude_code", cc_result, work_volume, image)
        if cc_result.exit_code != 0:
            duration = int((datetime.now(timezone.utc) - start_time).total_seconds())
            task.result = TaskResult(
                status=TaskStatus.FAILED,
                error=f"Claude Code exited with code {cc_result.exit_code}:\n{cc_result.logs[-2000:]}",
                duration_seconds=duration,
                finished=datetime.now(timezone.utc),
            )
            fail_task(self.config.tasks_dir, task_file, task)
            return

        commit_result = self._commit_changes(work_volume, image)
        self._log_step("commit", commit_result, work_volume, image)

        # Post-task hooks
        draft = False
        hook_output = None
        post_passed = True
        fix_attempts = 0

        if project.post_task:
            post_result = self.hook_runner.run_hooks(
                project.post_task, work_volume, project
            )

            if not post_result.success:
                post_passed = False
                policy = project.post_task_policy

                if policy == "fail":
                    duration = int(
                        (datetime.now(timezone.utc) - start_time).total_seconds()
                    )
                    task.result = TaskResult(
                        status=TaskStatus.FAILED,
                        error=f"Post-task hooks failed:\n{post_result.output}",
                        duration_seconds=duration,
                        finished=datetime.now(timezone.utc),
                        post_task_passed=False,
                    )
                    fail_task(self.config.tasks_dir, task_file, task)
                    return

                if policy in ("fix_or_fail", "fix_or_warn"):
                    post_result = self.fix_loop.run(
                        task, project, work_volume, post_result
                    )
                    fix_attempts = project.max_fix_attempts

                    if post_result.success:
                        post_passed = True
                    elif policy == "fix_or_fail":
                        duration = int(
                            (datetime.now(timezone.utc) - start_time).total_seconds()
                        )
                        task.result = TaskResult(
                            status=TaskStatus.FAILED,
                            error=f"Post-task hooks failed after {fix_attempts} fix attempts:\n{post_result.output}",
                            duration_seconds=duration,
                            finished=datetime.now(timezone.utc),
                            post_task_passed=False,
                            fix_attempts=fix_attempts,
                        )
                        fail_task(self.config.tasks_dir, task_file, task)
                        return
                    else:
                        # fix_or_warn: proceed with draft PR
                        draft = True
                        hook_output = post_result.output

                if policy == "warn" and not post_result.success:
                    draft = True
                    hook_output = post_result.output

        # Check if there are actual changes before pushing
        if not self._has_changes(work_volume, task.ref, image):
            duration = int((datetime.now(timezone.utc) - start_time).total_seconds())
            task.result = TaskResult(
                status=TaskStatus.COMPLETED,
                error="No changes produced",
                duration_seconds=duration,
                finished=datetime.now(timezone.utc),
                post_task_passed=post_passed,
            )
            complete_task(self.config.tasks_dir, task_file, task)
            return

        # Get diff
        diff = self._get_diff(work_volume, image)

        # Generate PR metadata
        pr_meta = self.summary_generator.generate(
            task,
            diff,
            hook_output,
            project=project,
            work_dir=work_volume,
            session_dir=session_volume,
        )
        self._log_step(
            "summary",
            ContainerResult(
                exit_code=0, logs=f"title={pr_meta.title} branch={pr_meta.branch}"
            ),
            work_volume,
            image,
        )

        # Rename branch
        self._rename_branch(work_volume, branch, pr_meta.branch, image)

        # Push via container
        push_result = self.docker_runner.run_git(
            command=f"git push origin {pr_meta.branch}",
            image=image,
            work_volume=work_volume,
            environment={"GIT_TERMINAL_PROMPT": "0"},
        )
        self._log_step("push", push_result, work_volume, image)
        if push_result.exit_code != 0:
            raise RuntimeError(
                f"Git push failed (exit {push_result.exit_code}):\n{push_result.logs}"
            )

        # Create GitHub client for API operations
        gh = GitHubClient(
            token=github_token or "",
            repo=self._extract_repo(task.repo),
        )

        pr_data = gh.create_pr(
            title=pr_meta.title,
            body=pr_meta.description,
            branch=pr_meta.branch,
            base=task.ref,
            draft=draft,
        )

        pr_number = pr_data["number"]

        # Labels
        done_label = project.github_labels.done
        needs_attention_label = project.github_labels.needs_attention

        if draft:
            gh.add_labels(pr_number, [needs_attention_label])
        else:
            gh.add_labels(pr_number, [done_label])

        # Update source issue/PR labels
        source_number = task.source.issue_number or task.source.pr_number
        if source_number:
            try:
                gh.remove_label(source_number, project.github_labels.trigger)
            except Exception:
                logger.debug("Could not remove trigger label from #%s", source_number)
            label = needs_attention_label if draft else done_label
            gh.add_labels(source_number, [label])

        # Complete task
        duration = int((datetime.now(timezone.utc) - start_time).total_seconds())
        task.result = TaskResult(
            status=TaskStatus.COMPLETED,
            pr_url=pr_data.get("html_url"),
            pr_number=pr_number,
            branch=pr_meta.branch,
            duration_seconds=duration,
            finished=datetime.now(timezone.utc),
            post_task_passed=post_passed,
            fix_attempts=fix_attempts if fix_attempts > 0 else None,
        )
        complete_task(self.config.tasks_dir, task_file, task)

    def _create_wowkmang_dir(self, work_volume: str, image: str) -> None:
        """Create .wowkmang directory in the workdir for step logs."""
        self.docker_runner.run_git(
            command="mkdir -p /workspace/.wowkmang",
            image=image,
            work_volume=work_volume,
        )

    def _log_step(
        self,
        step_name: str,
        result: ContainerResult,
        work_volume: str,
        image: str,
    ) -> None:
        """Log step output at DEBUG level and append to .wowkmang/steps.log."""
        logger.debug(
            "Step [%s] exit_code=%d\n%s", step_name, result.exit_code, result.logs
        )
        log_entry = (
            f"=== {step_name} (exit_code={result.exit_code}) ===\n" f"{result.logs}\n\n"
        )
        self.docker_runner.run_git(
            command=f"sh -c {shlex.quote(f'printf %s {shlex.quote(log_entry)} >> /workspace/.wowkmang/steps.log')}",
            image=image,
            work_volume=work_volume,
        )

    def _has_changes(self, work_volume: str, ref: str, image: str) -> bool:
        """Check if branch has changes compared to origin/{ref}. Returns True if changes exist."""
        result = self.docker_runner.run_git(
            command=f"git diff origin/{ref}..HEAD --quiet",
            image=image,
            work_volume=work_volume,
        )
        # exit code 0 = no diff, 1 = has changes
        return result.exit_code != 0

    def _recover_stale_tasks(self) -> None:
        """Kill orphaned containers and move running tasks back to pending or failed."""
        self.docker_runner.kill_stale_containers()

        running_dir = self.config.tasks_dir / "running"
        pending_dir = self.config.tasks_dir / "pending"
        failed_dir = self.config.tasks_dir / "failed"

        if not running_dir.exists():
            return

        for task_file in running_dir.glob("*.yaml"):
            task = task_from_yaml(task_file.read_text())
            task.attempts += 1

            if task.attempts < task.max_attempts:
                task_file.write_text(task_to_yaml(task))
                dest = pending_dir / task_file.name
                task_file.rename(dest)
                logger.info(
                    "Recovered task %s back to pending (attempt %d)",
                    task.id,
                    task.attempts,
                )
            else:
                task.result = TaskResult(
                    status=TaskStatus.FAILED,
                    error="Abandoned after crash recovery — max attempts reached",
                    finished=datetime.now(timezone.utc),
                )
                task_file.write_text(task_to_yaml(task))
                dest = failed_dir / task_file.name
                task_file.rename(dest)
                logger.info(
                    "Failed task %s after crash recovery — max attempts", task.id
                )

    def _seed_claude_config(self, session_volume: str, image: str) -> None:
        """Copy host claude config into session volume so the container has auth credentials."""
        source = self.config.host_claude_config_dir
        if not source:
            logger.warning(
                "No host_claude_config_dir configured, container may lack auth"
            )
            return
        self.docker_runner.seed_volume(
            image=image,
            source_host_path=source,
            target_volume=session_volume,
            target_path="/target",
        )
        logger.debug("Seeded session volume with claude config from %s", source)

    def _commit_changes(self, work_volume: str, image: str) -> ContainerResult:
        """Stage and commit any files Claude left uncommitted."""
        script = (
            "git -c user.name=wowkmang -c user.email=wowkmang@noreply "
            "add -A && "
            "git -c user.name=wowkmang -c user.email=wowkmang@noreply "
            "commit -m 'Apply Claude Code changes' || "
            "test \"$(git status --porcelain)\" = ''"
        )
        result = self.docker_runner.run_git(
            command=script,
            image=image,
            work_volume=work_volume,
        )
        if result.exit_code != 0:
            raise RuntimeError(
                f"Commit failed (exit {result.exit_code}):\n{result.logs}"
            )
        return result

    def _get_diff(self, work_volume: str, image: str) -> str:
        """Get diff of changes via container."""
        result = self.docker_runner.run_git(
            command="git diff HEAD~1..HEAD || git diff --cached",
            image=image,
            work_volume=work_volume,
        )
        return result.logs

    def _rename_branch(
        self, work_volume: str, old_branch: str, new_branch: str, image: str
    ) -> None:
        """Rename branch via container."""
        result = self.docker_runner.run_git(
            command=f"git branch -m {old_branch} {new_branch}",
            image=image,
            work_volume=work_volume,
        )
        if result.exit_code != 0:
            raise RuntimeError(
                f"Branch rename failed (exit {result.exit_code}):\n{result.logs}"
            )

    @staticmethod
    def _extract_repo(repo_url: str) -> str:
        """Extract 'user/project' from a GitHub URL."""
        cleaned = repo_url.replace("https://github.com/", "")
        if cleaned.endswith(".git"):
            cleaned = cleaned[:-4]
        return cleaned

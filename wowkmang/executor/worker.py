import json
import logging
import shlex
import threading
import time
from datetime import datetime, timezone
from enum import Enum

from wowkmang.api.config import GlobalConfig, ProjectConfig
from wowkmang.executor.docker_runner import ContainerResult, DockerRunner
from wowkmang.executor.github_client import GitHubClient
from wowkmang.executor.hooks import HookResult, HookRunner, HookType
from wowkmang.executor.prompts import build_task_prompt
from wowkmang.executor.result_file import (
    RESULT_FILE_PATH,
    TaskOutput,
    parse_result_file,
)
from wowkmang.executor.summary import PRMetadata, fallback_metadata
from wowkmang.taskqueue.models import (
    Task,
    TaskResult,
    TaskStatus,
    task_from_yaml,
    task_to_yaml,
)
from wowkmang.taskqueue.task_queue import (
    complete_task,
    fail_task,
    pick_next_task,
    prune_old_tasks,
    wait_for_input,
)
from wowkmang.executor.repo_cache import RepoCache

logger = logging.getLogger(__name__)


class FixLoop:
    def __init__(self, docker_runner: DockerRunner, hook_runner: HookRunner):
        self.docker_runner = docker_runner
        self.hook_runner = hook_runner

    def run(
        self,
        task,
        project: ProjectConfig,
        work_dir: str,
        project_volume: str,
        hook_failure: HookResult,
    ) -> HookResult:
        """Attempt to fix post-task check failures. Returns final check result."""
        last_result = hook_failure

        for attempt in range(project.max_fix_attempts):
            fix_prompt = (
                "The following checks failed after your changes. "
                f"Fix the issues:\n\n{last_result.output}"
            )

            model = task.model or project.default_model
            self.docker_runner.run_claude_code(
                work_dir=work_dir,
                project_volume=project_volume,
                task_prompt=fix_prompt,
                model=model,
                project=project,
                timeout_minutes=project.timeout_minutes,
                continue_session=True,
            )

            last_result = self.hook_runner.run_post_task_checks(
                work_dir, project_volume, project
            )

            if last_result.success:
                return last_result

        return last_result


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
    ):
        self.config = config
        self.projects = projects
        self.docker_runner = docker_runner
        self.repo_cache = repo_cache
        self.hook_runner = hook_runner
        self.fix_loop = fix_loop
        self._status = WorkerStatus.IDLE
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_prune: datetime | None = None

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
                    self._fail_task(task_file, task)
                finally:
                    self._status = WorkerStatus.IDLE
            else:
                self._maybe_prune()
                time.sleep(5)

    def _process_task(self, task_file, task: Task) -> None:
        logger.info("Starting task %s [%s]: %s", task.id, task.project, task.task[:80])
        project = self.projects.get(task.project)
        if not project:
            task.result = TaskResult(
                status=TaskStatus.FAILED,
                error=f"Unknown project: {task.project}",
                finished=datetime.now(timezone.utc),
            )
            self._fail_task(task_file, task)
            return

        start_time = datetime.now(timezone.utc)
        work_volume = self.docker_runner.create_volume(
            prefix="wowkmang-work", suffix=task.id
        )
        project_volume = self.docker_runner.ensure_project_volume(project.name)

        try:
            self._run_task_pipeline(
                task_file, task, project, work_volume, project_volume, start_time
            )
        except Exception as e:
            logger.exception("Unhandled error processing task %s", task.id)
            task.result = TaskResult(
                status=TaskStatus.FAILED,
                error=f"Internal error: {e}",
                finished=datetime.now(timezone.utc),
            )
            self._fail_task(task_file, task)
        finally:
            image = self.docker_runner.resolve_image(
                self.projects.get(task.project)
                or ProjectConfig(name=task.project, repo=task.repo)
            )
            logs = self._collect_logs(work_volume, project_volume, image)
            if task.result and logs:
                task.result.logs = logs
                self._save_task_result(task)
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
        project_volume: str,
        start_time: datetime,
    ) -> None:
        github_token = project.github_token or self.config.github_token
        image = self.docker_runner.resolve_image(project)

        # Remove trigger label early to prevent re-triggering
        self._remove_trigger_label(task, project, github_token)

        effective_uid = project.container_uid or self.config.container_uid

        # Setup environment: image, credentials, volumes, gitignore, repo
        self._setup_environment(
            task,
            project,
            work_volume,
            project_volume,
            image,
            github_token,
            effective_uid,
        )
        branch = self.repo_cache.prepare_workdir(
            task.repo,
            task.ref,
            work_volume,
            project_volume,
            image,
            github_token,
            existing_branch=task.pr_branch,
        )
        self._configure_git(work_volume, project_volume, image, project)

        # Pre-task hooks
        if project.pre_task:
            pre_result = self.hook_runner.run_hooks(
                HookType.PRE, work_volume, project_volume, project
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
                self._fail_task(task_file, task)
                return

        # Run Claude Code
        cc_result = self._execute_claude_code(
            task, project, work_volume, project_volume, image
        )
        self._log_step("claude_code", cc_result, work_volume, project_volume, image)
        if cc_result.exit_code != 0:
            duration = int((datetime.now(timezone.utc) - start_time).total_seconds())
            task.result = TaskResult(
                status=TaskStatus.FAILED,
                error=f"Claude Code exited with code {cc_result.exit_code}:\n{cc_result.logs[-2000:]}",
                duration_seconds=duration,
                finished=datetime.now(timezone.utc),
            )
            self._fail_task(task_file, task)
            return

        task_output = self._read_result_file(work_volume, project_volume, image)

        # Post-task checks
        check_result = self._run_post_checks(
            task_file, task, project, work_volume, project_volume, start_time
        )
        if check_result is None:
            return  # task was failed by _run_post_checks
        draft, post_passed, fix_attempts = check_result

        # Handle no-changes case
        if not self._has_any_changes(work_volume, project_volume, task.ref, image):
            self._handle_no_changes(
                task_file, task, task_output, github_token, start_time, post_passed
            )
            return

        # Commit, push, create PR, update labels, complete
        self._publish_and_complete(
            task_file,
            task,
            task_output,
            project,
            work_volume,
            project_volume,
            image,
            branch,
            github_token,
            draft,
            post_passed,
            fix_attempts,
            start_time,
        )

    def _setup_environment(
        self,
        task: Task,
        project: ProjectConfig,
        work_volume: str,
        project_volume: str,
        image: str,
        github_token: str,
        effective_uid: str,
    ) -> None:
        """Pull image, seed credentials, chown volumes, setup gitignore, create log dir."""
        self.docker_runner.ensure_image(image, project)
        self._seed_credentials(project_volume, image, effective_uid)
        self.docker_runner.chown_volume(
            image=image, work_volume=work_volume, uid=effective_uid
        )
        self.docker_runner.chown_project_volume(
            image=image, project_volume=project_volume, uid=effective_uid
        )
        self.docker_runner.setup_global_gitignore(
            project_volume=project_volume, image=image, uid=effective_uid
        )
        self._create_wowkmang_dir(work_volume, project_volume, image)

    def _execute_claude_code(
        self,
        task: Task,
        project: ProjectConfig,
        work_volume: str,
        project_volume: str,
        image: str,
    ) -> ContainerResult:
        """Build prompt and run Claude Code."""
        comments = self._read_comments_file(task.comments_file)
        prompt = build_task_prompt(task, project, comments)
        model = task.model or project.default_model
        return self.docker_runner.run_claude_code(
            work_dir=work_volume,
            project_volume=project_volume,
            task_prompt=prompt,
            model=model,
            project=project,
            timeout_minutes=project.timeout_minutes,
        )

    def _run_post_checks(
        self,
        task_file,
        task: Task,
        project: ProjectConfig,
        work_volume: str,
        project_volume: str,
        start_time: datetime,
    ) -> tuple[bool, bool, int] | None:
        """Run post-task checks and fix loop. Returns (draft, post_passed, fix_attempts) or None if task was failed."""
        draft = False
        post_passed = True
        fix_attempts = 0
        post_result = self.hook_runner.run_post_task_checks(
            work_volume, project_volume, project
        )

        if post_result.success:
            return draft, post_passed, fix_attempts

        post_passed = False
        policy = project.post_task_policy

        if policy == "fail":
            duration = int((datetime.now(timezone.utc) - start_time).total_seconds())
            task.result = TaskResult(
                status=TaskStatus.FAILED,
                error=f"Post-task checks failed:\n{post_result.output}",
                duration_seconds=duration,
                finished=datetime.now(timezone.utc),
                post_task_passed=False,
            )
            self._fail_task(task_file, task)
            return None

        if policy in ("fix_or_fail", "fix_or_warn"):
            post_result = self.fix_loop.run(
                task, project, work_volume, project_volume, post_result
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
                    error=f"Post-task checks failed after {fix_attempts} fix attempts:\n{post_result.output}",
                    duration_seconds=duration,
                    finished=datetime.now(timezone.utc),
                    post_task_passed=False,
                    fix_attempts=fix_attempts,
                )
                self._fail_task(task_file, task)
                return None
            else:
                draft = True

        if policy == "warn" and not post_result.success:
            draft = True

        return draft, post_passed, fix_attempts

    def _handle_no_changes(
        self,
        task_file,
        task: Task,
        task_output: TaskOutput | None,
        github_token: str,
        start_time: datetime,
        post_passed: bool,
    ) -> None:
        """Handle the case where Claude produced no code changes."""
        self._handle_comment(task_output, task, github_token)
        if task_output and task_output.questions and task.allow_questions:
            self._handle_questions(
                task_file, task, task_output, start_time, post_passed
            )
            return

        duration = int((datetime.now(timezone.utc) - start_time).total_seconds())
        task.result = TaskResult(
            status=TaskStatus.COMPLETED,
            error="No changes produced",
            duration_seconds=duration,
            finished=datetime.now(timezone.utc),
            post_task_passed=post_passed,
        )
        self._complete_task(task_file, task)

    def _publish_and_complete(
        self,
        task_file,
        task: Task,
        task_output: TaskOutput | None,
        project: ProjectConfig,
        work_volume: str,
        project_volume: str,
        image: str,
        branch: str,
        github_token: str,
        draft: bool,
        post_passed: bool,
        fix_attempts: int,
        start_time: datetime,
    ) -> None:
        """Commit changes, push, create PR, update labels, and complete task."""
        # Determine PR metadata from result file or fallback
        if task_output and task_output.commit:
            branch_name = task_output.commit.branch_name
            if task.pr_branch:
                pr_branch = task.pr_branch
            else:
                pr_branch = f"wowkmang/{branch_name}"
            pr_meta = PRMetadata(
                title=task_output.commit.title,
                branch=pr_branch,
                description=task_output.commit.description
                or f"Automated changes for: {task.task}",
            )
        else:
            pr_meta = fallback_metadata(task)

        self._log_step(
            "result_file",
            ContainerResult(
                exit_code=0, logs=f"title={pr_meta.title} branch={pr_meta.branch}"
            ),
            work_volume,
            project_volume,
            image,
        )

        # Commit
        commit_result = self._commit_changes(
            work_volume, project_volume, image, commit_message=pr_meta.title
        )
        self._log_step("commit", commit_result, work_volume, project_volume, image)

        # Rename branch (skip if using existing PR branch)
        if not task.pr_branch:
            self._rename_branch(
                work_volume, project_volume, branch, pr_meta.branch, image
            )

        # Push
        push_result = self.docker_runner.run_command(
            work_dir=work_volume,
            project_volume=project_volume,
            command=["git", "push", "origin", pr_meta.branch],
            image=image,
            environment={"GIT_TERMINAL_PROMPT": "0"},
        )
        self._log_step("push", push_result, work_volume, project_volume, image)
        if push_result.exit_code != 0:
            raise RuntimeError(
                f"Git push failed (exit {push_result.exit_code}):\n{push_result.logs}"
            )

        gh = GitHubClient(
            token=github_token or "",
            repo=self._extract_repo(task.repo),
        )

        self._handle_comment(task_output, task, github_token)

        # Create PR or use existing branch
        if task.pr_branch:
            pr_number = task.source.pr_number
            pr_url = None
        else:
            pr_data = gh.create_pr(
                title=pr_meta.title,
                body=pr_meta.description,
                branch=pr_meta.branch,
                base=task.ref,
                draft=draft,
            )
            pr_number = pr_data["number"]
            pr_url = pr_data.get("html_url")

            done_label = project.github_labels.done
            needs_attention_label = project.github_labels.needs_attention
            if draft:
                gh.add_labels(pr_number, [needs_attention_label])
            else:
                gh.add_labels(pr_number, [done_label])

        # Update source issue/PR labels
        source_number = task.source.issue_number or task.source.pr_number
        if source_number and not task.pr_branch:
            done_label = project.github_labels.done
            needs_attention_label = project.github_labels.needs_attention
            label = needs_attention_label if draft else done_label
            gh.add_labels(source_number, [label])

        # Handle questions (after commit/push)
        if task_output and task_output.questions and task.allow_questions:
            self._handle_questions(
                task_file,
                task,
                task_output,
                start_time,
                post_passed,
                pr_url=pr_url,
                pr_number=pr_number,
                branch=pr_meta.branch,
                fix_attempts=fix_attempts,
            )
            return

        # Complete task
        duration = int((datetime.now(timezone.utc) - start_time).total_seconds())
        task.result = TaskResult(
            status=TaskStatus.COMPLETED,
            pr_url=pr_url,
            pr_number=pr_number,
            branch=pr_meta.branch,
            duration_seconds=duration,
            finished=datetime.now(timezone.utc),
            post_task_passed=post_passed,
            fix_attempts=fix_attempts if fix_attempts > 0 else None,
        )
        self._complete_task(task_file, task)

    def _read_result_file(
        self, work_volume: str, project_volume: str, image: str
    ) -> TaskOutput | None:
        """Read and parse .claude-result.json from work volume."""
        try:
            raw = self.docker_runner.read_file(
                volume=work_volume,
                path=RESULT_FILE_PATH,
                image=image,
                mount_point="/workspace",
            )
            if not raw:
                return None
            return parse_result_file(raw)
        except Exception:
            logger.warning("Could not read/parse .claude-result.json")
            return None

    def _read_comments_file(self, comments_file: str | None) -> str | None:
        """Read comments context file from disk."""
        if not comments_file:
            return None
        try:
            from pathlib import Path

            path = Path(comments_file)
            if path.exists():
                data = json.loads(path.read_text())
                parts = []
                for c in data:
                    user = c.get("user", "unknown")
                    body = c.get("body", "")
                    path_info = c.get("path", "")
                    header = f"**@{user}**"
                    if path_info:
                        header += f" (on `{path_info}`)"
                    parts.append(f"{header}:\n{body}")
                return "\n\n---\n\n".join(parts)
        except Exception:
            logger.warning("Could not read comments file: %s", comments_file)
        return None

    def _handle_comment(
        self,
        task_output: TaskOutput | None,
        task: Task,
        github_token: str | None,
    ) -> None:
        """Post a comment on the source issue/PR if result file contains one."""
        if not task_output or not task_output.comment:
            return
        source_number = task.source.issue_number or task.source.pr_number
        if not source_number:
            return
        try:
            gh = GitHubClient(
                token=github_token or "",
                repo=self._extract_repo(task.repo),
            )
            gh.create_comment(source_number, task_output.comment.message)
        except Exception:
            logger.warning("Could not post comment to #%s", source_number)

    def _handle_questions(
        self,
        task_file,
        task: Task,
        task_output: TaskOutput,
        start_time: datetime,
        post_passed: bool,
        pr_url: str | None = None,
        pr_number: int | None = None,
        branch: str | None = None,
        fix_attempts: int = 0,
    ) -> None:
        """Move task to waiting state with questions."""
        questions = [
            {"message": q.message, "choices": q.choices} for q in task_output.questions
        ]

        # Post questions as comment on source issue/PR
        source_number = task.source.issue_number or task.source.pr_number
        if source_number:
            github_token = (
                self.projects.get(
                    task.project, ProjectConfig(name="_", repo="")
                ).github_token
                or self.config.github_token
            )
            try:
                gh = GitHubClient(
                    token=github_token or "",
                    repo=self._extract_repo(task.repo),
                )
                comment_parts = ["I have some questions before I can proceed:\n"]
                for q in task_output.questions:
                    comment_parts.append(f"- {q.message}")
                    if q.choices:
                        for choice in q.choices:
                            comment_parts.append(f"  - {choice}")
                gh.create_comment(source_number, "\n".join(comment_parts))
            except Exception:
                logger.warning("Could not post questions to #%s", source_number)

        duration = int((datetime.now(timezone.utc) - start_time).total_seconds())
        task.result = TaskResult(
            status=TaskStatus.WAITING_FOR_INPUT,
            pr_url=pr_url,
            pr_number=pr_number,
            branch=branch,
            duration_seconds=duration,
            finished=datetime.now(timezone.utc),
            post_task_passed=post_passed,
            fix_attempts=fix_attempts if fix_attempts > 0 else None,
            questions=questions,
        )
        wait_for_input(self.config.tasks_dir, task_file, task)

    def _remove_trigger_label(
        self, task: Task, project: ProjectConfig, github_token: str
    ) -> None:
        """Remove the trigger label from the source issue/PR to prevent re-triggering."""
        source_number = task.source.issue_number or task.source.pr_number
        if not source_number:
            return
        try:
            gh = GitHubClient(
                token=github_token or "",
                repo=self._extract_repo(task.repo),
            )
            gh.remove_label(source_number, project.github_labels.trigger)
        except Exception:
            logger.debug("Could not remove trigger label from #%s", source_number)

    def _create_wowkmang_dir(
        self, work_volume: str, project_volume: str, image: str
    ) -> None:
        """Create .wowkmang directory in the workdir for step logs."""
        self.docker_runner.run_command(
            work_dir=work_volume,
            project_volume=project_volume,
            command=["mkdir", "-p", "/workspace/.wowkmang"],
            image=image,
        )

    def _log_step(
        self,
        step_name: str,
        result: ContainerResult,
        work_volume: str,
        project_volume: str,
        image: str,
    ) -> None:
        """Log step output at DEBUG level and append to .wowkmang/steps.log."""
        logger.info(
            "Step [%s] exit_code=%d\n%s", step_name, result.exit_code, result.logs
        )
        log_entry = (
            f"=== {step_name} (exit_code={result.exit_code}) ===\n" f"{result.logs}\n\n"
        )
        self.docker_runner.run_command(
            work_dir=work_volume,
            project_volume=project_volume,
            command=[
                "sh",
                "-c",
                f"printf %s {shlex.quote(log_entry)} >> /workspace/.wowkmang/steps.log",
            ],
            image=image,
        )

    def _collect_logs(self, work_volume: str, project_volume: str, image: str) -> str:
        """Read steps.log from the work volume."""
        try:
            return self.docker_runner.read_file(
                volume=work_volume,
                path=".wowkmang/steps.log",
                image=image,
                mount_point="/workspace",
            )
        except Exception:
            logger.debug("Could not collect steps.log from %s", work_volume)
            return ""

    def _save_task_result(self, task: Task) -> None:
        """Re-save task file after updating result (e.g. adding logs)."""
        for subdir in ("done", "failed", "running", "pending", "waiting"):
            for path in (self.config.tasks_dir / subdir).glob(f"*_{task.id}.yaml"):
                path.write_text(task_to_yaml(task))
                return

    def _has_changes(
        self, work_volume: str, project_volume: str, ref: str, image: str
    ) -> bool:
        """Check if branch has changes compared to origin/{ref}. Returns True if changes exist."""
        result = self.docker_runner.run_command(
            work_dir=work_volume,
            project_volume=project_volume,
            command=["git", "diff", f"origin/{ref}..HEAD", "--quiet"],
            image=image,
        )
        # exit code 0 = no diff, 1 = has changes
        return result.exit_code != 0

    def _maybe_prune(self) -> None:
        """Prune old finished tasks at most once per hour."""
        now = datetime.now(timezone.utc)
        if (
            self._last_prune is not None
            and (now - self._last_prune).total_seconds() < 3600
        ):
            return
        self._last_prune = now
        retention = self.config.task_retention_days
        deleted = prune_old_tasks(self.config.tasks_dir, retention)
        if deleted:
            logger.info("Pruned %d task(s) older than %d day(s)", deleted, retention)

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

    def _seed_credentials(self, project_volume: str, image: str, uid: str) -> None:
        """Copy credentials.json from host claude config into project volume."""
        source = self.config.host_claude_config_dir
        if not source:
            logger.warning(
                "No host_claude_config_dir configured, container may lack auth"
            )
            return
        self.docker_runner.seed_credentials(
            image=image,
            source_dir=source,
            project_volume=project_volume,
        )
        logger.debug("Seeded project volume with credentials from %s", source)

    def _configure_git(
        self, work_volume: str, project_volume: str, image: str, project: ProjectConfig
    ) -> None:
        """Configure git user identity in the cloned repo."""
        git_name = project.git_name or self.config.git_name
        git_email = project.git_email or self.config.git_email
        script = (
            f"git config user.name {shlex.quote(git_name)} && "
            f"git config user.email {shlex.quote(git_email)}"
        )
        self.docker_runner.run_command(
            work_dir=work_volume,
            project_volume=project_volume,
            command=["sh", "-c", script],
            image=image,
        )

    def _commit_changes(
        self,
        work_volume: str,
        project_volume: str,
        image: str,
        commit_message: str = "Apply Claude Code changes",
    ) -> ContainerResult:
        """Stage and commit any files Claude left uncommitted."""
        script = (
            f"git add -A && "
            f"git commit -m {shlex.quote(commit_message)} || "
            "test \"$(git status --porcelain)\" = ''"
        )
        result = self.docker_runner.run_command(
            work_dir=work_volume,
            project_volume=project_volume,
            command=["sh", "-c", script],
            image=image,
        )
        if result.exit_code != 0:
            raise RuntimeError(
                f"Commit failed (exit {result.exit_code}):\n{result.logs}"
            )
        return result

    def _has_any_changes(
        self, work_volume: str, project_volume: str, ref: str, image: str
    ) -> bool:
        """Check for committed changes beyond origin/{ref} or any uncommitted changes."""
        committed = self.docker_runner.run_command(
            work_dir=work_volume,
            project_volume=project_volume,
            command=["git", "diff", f"origin/{ref}..HEAD", "--quiet"],
            image=image,
        )
        if committed.exit_code != 0:
            return True
        uncommitted = self.docker_runner.run_command(
            work_dir=work_volume,
            project_volume=project_volume,
            command=["sh", "-c", '[ -z "$(git status --porcelain)" ]'],
            image=image,
        )
        return uncommitted.exit_code != 0

    def _get_diff_before_commit(
        self, work_volume: str, project_volume: str, ref: str, image: str
    ) -> str:
        """Get the full diff of all changes (committed and uncommitted) vs origin/{ref}."""
        result = self.docker_runner.run_command(
            work_dir=work_volume,
            project_volume=project_volume,
            command=[
                "sh",
                "-c",
                f"git diff origin/{ref}..HEAD; git diff HEAD",
            ],
            image=image,
        )
        return result.logs

    def _rename_branch(
        self,
        work_volume: str,
        project_volume: str,
        old_branch: str,
        new_branch: str,
        image: str,
    ) -> None:
        """Rename branch via container."""
        result = self.docker_runner.run_command(
            work_dir=work_volume,
            project_volume=project_volume,
            command=["git", "branch", "-m", old_branch, new_branch],
            image=image,
        )
        if result.exit_code != 0:
            raise RuntimeError(
                f"Branch rename failed (exit {result.exit_code}):\n{result.logs}"
            )

    def _fail_task(self, task_file, task: Task) -> None:
        logger.info(
            "Task %s [%s] failed: %s",
            task.id,
            task.project,
            (task.result.error or "")[:120] if task.result else "unknown",
        )
        fail_task(self.config.tasks_dir, task_file, task)

    def _complete_task(self, task_file, task: Task) -> None:
        result = task.result
        logger.info(
            "Task %s [%s] completed: pr=%s status=%s",
            task.id,
            task.project,
            result.pr_url if result else None,
            result.status if result else None,
        )
        complete_task(self.config.tasks_dir, task_file, task)

    @staticmethod
    def _extract_repo(repo_url: str) -> str:
        """Extract 'user/project' from a GitHub URL."""
        cleaned = repo_url.replace("https://github.com/", "")
        if cleaned.endswith(".git"):
            cleaned = cleaned[:-4]
        return cleaned

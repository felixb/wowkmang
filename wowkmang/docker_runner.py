import logging
import shlex
import uuid

from pydantic import BaseModel

from wowkmang.config import ProjectConfig

logger = logging.getLogger(__name__)


class ContainerResult(BaseModel):
    exit_code: int
    logs: str


class DockerRunner:
    CONTAINER_LABEL = "wowkmang"
    VOLUME_LABEL = "wowkmang"

    def __init__(self, docker_client, cache_volume: str, pull_token: str = ""):
        self.client = docker_client
        self.cache_volume = cache_volume
        self.pull_token = pull_token
        self._pulled_images: set[str] = set()

    def ensure_image(self, image: str, project: ProjectConfig) -> None:
        """Pull a Docker image, trying multiple auth strategies.

        Order: global pull_token -> project github_token -> unauthenticated.
        Skips if the image was already pulled in this session.
        """
        if image in self._pulled_images:
            return

        tokens: list[tuple[str, str]] = []
        if self.pull_token:
            tokens.append(("pull_token", self.pull_token))
        project_token = project.credentials.get(
            "GITHUB_TOKEN"
        ) or project.credentials.get("github_token")
        if project_token and project_token != self.pull_token:
            tokens.append(("project token", project_token))

        for label, token in tokens:
            try:
                self.client.images.pull(
                    image,
                    auth_config={"username": "x", "password": token},
                )
                logger.info("Pulled %s using %s", image, label)
                self._pulled_images.add(image)
                return
            except Exception:
                logger.debug("Failed to pull %s with %s, trying next", image, label)

        # Last resort: unauthenticated
        try:
            self.client.images.pull(image)
            logger.info("Pulled %s without auth", image)
            self._pulled_images.add(image)
        except Exception:
            logger.warning("Could not pull %s — will try to use local image", image)

    def create_volume(self, prefix: str = "wowkmang", suffix: str | None = None) -> str:
        """Create a Docker named volume with wowkmang label. Returns volume name."""
        name = f"{prefix}-{suffix or uuid.uuid4().hex[:12]}"
        self.client.volumes.create(name=name, labels={self.VOLUME_LABEL: "true"})
        logger.debug("Created volume %s", name)
        return name

    def remove_volume(self, name: str) -> None:
        """Remove a Docker named volume."""
        try:
            volume = self.client.volumes.get(name)
            volume.remove()
            logger.debug("Removed volume %s", name)
        except Exception:
            logger.warning("Could not remove volume %s", name)

    def run_git(
        self,
        command: str,
        image: str,
        work_volume: str,
        environment: dict | None = None,
        timeout_seconds: int = 300,
    ) -> ContainerResult:
        """Run a shell command in a container with work volume at /workspace and cache at /cache."""
        safe_dirs = "git config --global --add safe.directory '*'"
        full_command = f"{safe_dirs} && {command}"
        volumes = {
            work_volume: {"bind": "/workspace", "mode": "rw"},
            self.cache_volume: {"bind": "/cache", "mode": "rw"},
        }
        return self._run_container(
            image=image,
            command=f"sh -c {shlex.quote(full_command)}",
            environment=environment or {},
            volumes=volumes,
            timeout_seconds=timeout_seconds,
        )

    def seed_volume(
        self,
        image: str,
        source_host_path: str,
        target_volume: str,
        target_path: str = "/target",
    ) -> ContainerResult:
        """Copy files from a host bind-mount into a volume via a short-lived container."""
        volumes = {
            source_host_path: {"bind": "/source", "mode": "ro"},
            target_volume: {"bind": target_path, "mode": "rw"},
        }
        return self._run_container(
            image=image,
            command="sh -c 'cp -a /source/. /target/'",
            environment={},
            volumes=volumes,
            timeout_seconds=60,
            working_dir="/",
        )

    def copy_to_workdir(
        self,
        work_volume: str,
        session_volume: str,
        cache_subdir: str,
        image: str,
    ) -> ContainerResult:
        """Copy bare repo cache and .claude session dir into workdir for self-contained debugging."""
        script = (
            f"mkdir -p /workspace/.cache && "
            f"cp -a /cache/{cache_subdir} /workspace/.cache/{cache_subdir} && "
            f"cp -a /session/. /workspace/.claude/"
        )
        volumes = {
            work_volume: {"bind": "/workspace", "mode": "rw"},
            self.cache_volume: {"bind": "/cache", "mode": "ro"},
            session_volume: {"bind": "/session", "mode": "ro"},
        }
        return self._run_container(
            image=image,
            command=f"sh -c {shlex.quote(script)}",
            environment={},
            volumes=volumes,
            timeout_seconds=120,
            working_dir="/workspace",
        )

    def run_claude_code(
        self,
        work_dir: str,
        task_prompt: str,
        model: str,
        project: ProjectConfig,
        timeout_minutes: int,
        session_dir: str | None = None,
        continue_session: bool = False,
        output_format: str | None = None,
    ) -> ContainerResult:
        """Spin up a Claude Code container and run a task."""

        full_prompt = task_prompt
        if project.extra_instructions and not continue_session:
            full_prompt = f"{project.extra_instructions}\n\n{task_prompt}"

        command = ["claude", "--dangerously-skip-permissions"]
        if continue_session:
            command.append("--continue")
        command.extend(["--model", model, "--print"])
        if output_format:
            command.extend(["--output-format", output_format])
        command.append(full_prompt)

        environment = {
            "CLAUDE_MODEL": model,
            **project.credentials,
        }

        volumes = self._build_volumes(work_dir, session_dir=session_dir)

        return self._run_container(
            image=project.docker_image,
            command=command,
            environment=environment,
            volumes=volumes,
            timeout_seconds=timeout_minutes * 60,
        )

    def run_hooks(
        self,
        work_dir: str,
        commands: list[str],
        project: ProjectConfig,
    ) -> ContainerResult:
        """Run hook commands one by one in separate containers. Returns first failure or final success."""
        last_result = ContainerResult(exit_code=0, logs="")
        for cmd in commands:
            command = f"sh -c {shlex.quote(cmd)}"
            environment = {**project.credentials}
            volumes = self._build_volumes(work_dir)

            last_result = self._run_container(
                image=project.docker_image,
                command=command,
                environment=environment,
                volumes=volumes,
                timeout_seconds=project.timeout_minutes * 60,
            )
            if last_result.exit_code != 0:
                return last_result

        return last_result

    def kill_stale_containers(self) -> None:
        """Find and kill any orphaned wowkmang containers and volumes."""
        containers = self.client.containers.list(
            filters={"label": self.CONTAINER_LABEL}
        )
        for container in containers:
            try:
                container.kill()
                container.remove()
            except Exception:
                pass

        # Clean up orphaned volumes
        try:
            volumes = self.client.volumes.list(filters={"label": self.VOLUME_LABEL})
            for volume in volumes:
                try:
                    volume.remove()
                except Exception:
                    pass
        except Exception:
            pass

    def _build_volumes(self, work_dir: str, session_dir: str | None = None) -> dict:
        volumes = {
            work_dir: {"bind": "/workspace", "mode": "rw"},
            self.cache_volume: {"bind": "/cache", "mode": "rw"},
        }
        if session_dir:
            volumes[session_dir] = {"bind": "/root/.claude", "mode": "rw"}
        return volumes

    def _run_container(
        self,
        image: str,
        command: str | list[str],
        environment: dict,
        volumes: dict,
        timeout_seconds: int,
        working_dir: str = "/workspace/repo",
    ) -> ContainerResult:
        container = self.client.containers.run(
            image=image,
            command=command,
            environment=environment,
            volumes=volumes,
            working_dir=working_dir,
            detach=True,
            mem_limit="4g",
            cpu_period=100000,
            cpu_quota=200000,
            labels={self.CONTAINER_LABEL: "true"},
        )

        try:
            result = container.wait(timeout=timeout_seconds)
            logs = container.logs().decode()
            return ContainerResult(
                exit_code=result["StatusCode"],
                logs=logs,
            )
        except Exception:
            container.kill()
            raise
        finally:
            container.remove()

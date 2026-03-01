import logging
import uuid

from pydantic import BaseModel

from wowkmang.api.config import ProjectConfig

logger = logging.getLogger(__name__)


class ContainerResult(BaseModel):
    exit_code: int
    logs: str


class DockerRunner:
    CONTAINER_LABEL = "wowkmang"
    VOLUME_LABEL = "wowkmang"

    def __init__(
        self,
        docker_client,
        pull_token: str = "",
        github_token: str = "",
        default_uid: str = "1000:1000",
        default_docker_image: str = "",
    ):
        self.client = docker_client
        self.pull_token = pull_token
        self.github_token = github_token
        self.default_uid = default_uid
        self.default_docker_image = default_docker_image
        self._pulled_images: set[str] = set()

    def resolve_image(self, project: ProjectConfig) -> str:
        """Return the Docker image to use: project-specific if set, else global default."""
        return project.docker_image or self.default_docker_image

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

        project_token = project.github_token or self.github_token
        if project_token and project_token != self.pull_token:
            tokens.append(("github_token", project_token))

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

    def ensure_project_volume(self, project_name: str) -> str:
        """Get or create a persistent per-project Docker volume. Returns volume name.

        Project volumes are not labeled with wowkmang so kill_stale_containers()
        won't delete them.
        """
        name = f"wowkmang-project-{project_name}"
        try:
            self.client.volumes.get(name)
            logger.debug("Reusing project volume %s", name)
        except Exception:
            self.client.volumes.create(name=name)
            logger.debug("Created project volume %s", name)
        return name

    def setup_global_gitignore(
        self,
        project_volume: str,
        image: str,
        uid: str = "1000:1000",
    ) -> None:
        """Create a global gitignore excluding .claude-result.json on the project volume."""
        script = (
            "mkdir -p /cache && "
            "printf '%s\\n' .claude-result.json > /cache/.gitignore_global && "
            "git config --global core.excludesFile /cache/.gitignore_global"
        )
        volumes = {
            project_volume: {"bind": "/cache", "mode": "rw"},
        }
        self._run_container(
            image=image,
            command=["sh", "-c", script],
            environment={},
            volumes=volumes,
            timeout_seconds=30,
            working_dir="/",
            user=uid,
        )

    def remove_volume(self, name: str) -> None:
        """Remove a Docker named volume."""
        try:
            volume = self.client.volumes.get(name)
            volume.remove()
            logger.debug("Removed volume %s", name)
        except Exception:
            logger.warning("Could not remove volume %s", name)

    def seed_credentials(
        self,
        image: str,
        source_dir: str,
        project_volume: str,
    ) -> ContainerResult:
        """Copy credentials.json from host dir into /cache/.claude/ in project volume."""
        volumes = {
            source_dir: {"bind": "/source", "mode": "ro"},
            project_volume: {"bind": "/cache", "mode": "rw"},
        }
        return self._run_container(
            image=image,
            command=[
                "sh",
                "-c",
                "mkdir -p /cache/.claude && cp -a /source/.credentials.json /cache/.claude/",
            ],
            environment={},
            volumes=volumes,
            timeout_seconds=60,
            working_dir="/",
        )

    def chown_volume(
        self,
        image: str,
        work_volume: str,
        uid: str = "1000:1000",
    ) -> ContainerResult:
        """Chown workspace so non-root containers have a writable /workspace."""
        volumes = {
            work_volume: {"bind": "/workspace", "mode": "rw"},
        }
        script = f"chown -R {uid} /workspace"
        return self._run_container(
            image=image,
            command=["sh", "-c", script],
            environment={},
            volumes=volumes,
            timeout_seconds=120,
            working_dir="/",
        )

    def chown_project_volume(
        self,
        image: str,
        project_volume: str,
        uid: str = "1000:1000",
    ) -> ContainerResult:
        """Chown the entire /cache mount to container uid."""
        volumes = {
            project_volume: {"bind": "/cache", "mode": "rw"},
        }
        script = f"mkdir -p /cache && chown -R {uid} /cache"
        return self._run_container(
            image=image,
            command=["sh", "-c", script],
            environment={},
            volumes=volumes,
            timeout_seconds=120,
            working_dir="/",
        )

    def run_command(
        self,
        work_dir: str,
        project_volume: str,
        command: str | list[str],
        image: str,
        environment: dict | None = None,
        timeout_seconds: int = 300,
        user: str | None = None,
    ) -> ContainerResult:
        """Run a command in a container with wiring (volumes, etc)."""
        if user is None:
            user = self.default_uid
        volumes = self._build_volumes(work_dir, project_volume)
        return self._run_container(
            image=image,
            command=command,
            environment=environment or {},
            volumes=volumes,
            timeout_seconds=timeout_seconds,
            user=user,
        )

    def run_claude_code(
        self,
        work_dir: str,
        project_volume: str,
        task_prompt: str,
        model: str,
        project: ProjectConfig,
        timeout_minutes: int,
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
            "GITHUB_TOKEN": project.github_token or self.github_token,
        }

        return self.run_command(
            work_dir=work_dir,
            project_volume=project_volume,
            command=command,
            image=self.resolve_image(project),
            environment=environment,
            timeout_seconds=timeout_minutes * 60,
        )

    def run_hooks(
        self,
        work_dir: str,
        project_volume: str,
        commands: list[str],
        project: ProjectConfig,
    ) -> ContainerResult:
        """Run hook commands one by one in separate containers. Returns first failure or final success."""
        last_result = ContainerResult(exit_code=0, logs="")
        for cmd in commands:
            last_result = self.run_command(
                work_dir=work_dir,
                project_volume=project_volume,
                command=cmd,
                image=self.resolve_image(project),
                environment={"GITHUB_TOKEN": project.github_token or self.github_token},
                timeout_seconds=project.timeout_minutes * 60,
            )
            if last_result.exit_code != 0:
                return last_result

        return last_result

    def read_file(
        self,
        volume: str,
        path: str,
        image: str,
        mount_point: str = "/mnt",
    ) -> str:
        """Read a file from a Docker volume via a short-lived container."""
        result = self._run_container(
            image=image,
            command=["cat", f"{mount_point}/{path}"],
            environment={},
            volumes={volume: {"bind": mount_point, "mode": "ro"}},
            timeout_seconds=30,
            working_dir="/",
        )
        return result.logs if result.exit_code == 0 else ""

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

    def _build_volumes(self, work_dir: str, project_volume: str) -> dict:
        return {
            work_dir: {"bind": "/workspace", "mode": "rw"},
            project_volume: {"bind": "/cache", "mode": "rw"},
        }

    def _run_container(
        self,
        image: str,
        command: str | list[str],
        environment: dict,
        volumes: dict,
        timeout_seconds: int,
        working_dir: str = "/workspace/repo",
        user: str | None = None,
    ) -> ContainerResult:
        effective_environment = {"GIT_DISCOVERY_ACROSS_FILESYSTEM": "1"} | environment
        if user:
            effective_environment["HOME"] = "/cache"

        kwargs = dict(
            image=image,
            command=command,
            entrypoint="",
            environment=effective_environment,
            volumes=volumes,
            working_dir=working_dir,
            detach=True,
            mem_limit="4g",
            cpu_period=100000,
            cpu_quota=200000,
            labels={self.CONTAINER_LABEL: "true"},
        )
        if user:
            kwargs["user"] = user

        container = self.client.containers.run(**kwargs)

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

from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class GlobalConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WOWKMANG_", env_file=".env", env_file_encoding="utf-8"
    )

    host_claude_config_dir: str = ""
    projects_dir: Path = Path("./projects")
    tasks_dir: Path = Path("./tasks")
    host: str = "0.0.0.0"
    port: int = 8484
    api_tokens: str = ""
    pull_token: str = ""
    github_token: str = ""
    keep_workdir: bool = False
    container_uid: str = "1000:1000"
    git_name: str = "wowkmang"
    git_email: str = "wowkmang@noreply"


class GitHubLabels(BaseModel):
    trigger: str = "wowkmang"
    in_progress: str = "wowkmang/in-progress"
    done: str = "wowkmang/done"
    failed: str = "wowkmang/failed"
    needs_attention: str = "wowkmang/needs-attention"


class ProjectConfig(BaseModel):
    name: str
    repo: str
    ref: str = "main"
    github_token: str = ""
    git_name: str = ""
    git_email: str = ""
    default_model: str = "sonnet"
    extra_instructions: str = ""
    docker_image: str = "ghcr.io/anthropics/claude-code:latest"
    timeout_minutes: int = 30
    max_fix_attempts: int = 2
    pre_task: list[str] = Field(default_factory=list)
    post_task: list[str] = Field(default_factory=list)
    post_task_policy: str = "fix_or_warn"
    github_labels: GitHubLabels = Field(default_factory=GitHubLabels)
    webhook_secret: str = ""
    container_uid: str = ""


def load_projects(projects_dir: Path) -> dict[str, ProjectConfig]:
    projects: dict[str, ProjectConfig] = {}
    if not projects_dir.is_dir():
        return projects
    for path in sorted(projects_dir.glob("*.yaml")):
        with open(path) as f:
            data = yaml.safe_load(f)
        if data:
            project = ProjectConfig(**data)
            projects[project.name] = project
    return projects


def find_project_by_repo(
    repo_full_name: str, projects: dict[str, ProjectConfig]
) -> ProjectConfig | None:
    for project in projects.values():
        if repo_full_name in project.repo:
            return project
    return None

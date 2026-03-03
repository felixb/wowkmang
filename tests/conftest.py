import pytest
import yaml

from wowkmang.api.auth import hash_token
from wowkmang.api.config import GlobalConfig

SAMPLE_PROJECT = {
    "name": "testproject",
    "repo": "https://github.com/user/testproject",
    "ref": "main",
    "github_token": "ghp_test",
    "default_model": "claude-sonnet-4-5-20250929",
    "webhook_secret": "whsec_testsecret",
    "github_labels": {"trigger": "wowkmang"},
    "pre_task": ["echo pre"],
    "post_task": ["echo post"],
}

SAMPLE_PROJECT_WITH_ALLOWLIST = {
    "name": "allowedproject",
    "repo": "https://github.com/user/allowedproject",
    "ref": "main",
    "github_token": "ghp_test",
    "default_model": "claude-sonnet-4-5-20250929",
    "webhook_secret": "whsec_allowedsecret",
    "github_labels": {"trigger": "wowkmang"},
    "allowed_users": ["trusteduser", "admin"],
}

TEST_API_TOKEN = "test-token-abc123"
TEST_API_TOKEN_HASH = hash_token(TEST_API_TOKEN)


@pytest.fixture
def tmp_projects_dir(tmp_path):
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    (projects_dir / "testproject.yaml").write_text(yaml.dump(SAMPLE_PROJECT))
    (projects_dir / "allowedproject.yaml").write_text(
        yaml.dump(SAMPLE_PROJECT_WITH_ALLOWLIST)
    )
    return projects_dir


@pytest.fixture
def tmp_tasks_dir(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    for d in ["pending", "running", "done", "failed", "waiting"]:
        (tasks_dir / d).mkdir()
    return tasks_dir


@pytest.fixture
def global_config(tmp_projects_dir, tmp_tasks_dir):
    return GlobalConfig(
        projects_dir=tmp_projects_dir,
        tasks_dir=tmp_tasks_dir,
        api_tokens=TEST_API_TOKEN_HASH,
    )

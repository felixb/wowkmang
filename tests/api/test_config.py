import yaml

from wowkmang.api.config import (
    GlobalConfig,
    GitHubLabels,
    ProjectConfig,
    find_project_by_repo,
    load_projects,
)


class TestGlobalConfig:
    def test_defaults(self):
        config = GlobalConfig()
        assert config.host == "0.0.0.0"
        assert config.port == 8484
        assert config.container_uid == "1000:1000"
        assert config.docker_image == "ghcr.io/anthropics/claude-code:latest"

    def test_custom_values(self):
        config = GlobalConfig(
            port=9000,
            api_tokens="abc,def",
        )
        assert config.port == 9000
        assert config.api_tokens == "abc,def"


class TestProjectConfig:
    def test_defaults(self):
        p = ProjectConfig(name="test", repo="https://github.com/a/b")
        assert p.ref == "main"
        assert p.timeout_minutes == 30
        assert p.github_labels.trigger == "wowkmang"
        assert p.container_uid == ""
        assert p.docker_image == ""

    def test_full_config(self):
        p = ProjectConfig(
            name="myproj",
            repo="https://github.com/user/repo",
            ref="develop",
            timeout_minutes=60,
            github_labels=GitHubLabels(trigger="custom-label"),
        )
        assert p.ref == "develop"
        assert p.github_labels.trigger == "custom-label"


class TestLoadProjects:
    def test_load_projects(self, tmp_projects_dir):
        projects = load_projects(tmp_projects_dir)
        assert "testproject" in projects
        p = projects["testproject"]
        assert p.repo == "https://github.com/user/testproject"
        assert p.webhook_secret == "whsec_testsecret"

    def test_load_projects_empty_dir(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert load_projects(empty) == {}

    def test_load_projects_missing_dir(self, tmp_path):
        assert load_projects(tmp_path / "nonexistent") == {}

    def test_load_multiple_projects(self, tmp_projects_dir):
        (tmp_projects_dir / "second.yaml").write_text(
            yaml.dump({"name": "second", "repo": "https://github.com/a/b"})
        )
        projects = load_projects(tmp_projects_dir)
        assert "second" in projects
        assert "testproject" in projects
        assert "allowedproject" in projects


class TestFindProjectByRepo:
    def test_find_existing(self, tmp_projects_dir):
        projects = load_projects(tmp_projects_dir)
        result = find_project_by_repo("user/testproject", projects)
        assert result is not None
        assert result.name == "testproject"

    def test_find_missing(self, tmp_projects_dir):
        projects = load_projects(tmp_projects_dir)
        assert find_project_by_repo("unknown/repo", projects) is None

    def test_no_substring_match(self, tmp_projects_dir):
        """A partial repo name must not match (security: prevents wrong project lookup)."""
        projects = load_projects(tmp_projects_dir)
        assert find_project_by_repo("user/test", projects) is None
        assert find_project_by_repo("ser/testproject", projects) is None

    def test_trailing_slash_ignored(self):
        projects = {"p": ProjectConfig(name="p", repo="https://github.com/org/repo/")}
        assert find_project_by_repo("org/repo", projects) is not None

import yaml

from wowkmang.config import (
    GlobalConfig,
    GitHubLabels,
    ProjectConfig,
    find_project_by_repo,
    load_projects,
)


class TestGlobalConfig:
    def test_defaults(self):
        config = GlobalConfig(
            _env_prefix="WOWKMANG_TEST_UNUSED_",
        )
        assert config.host == "0.0.0.0"
        assert config.port == 8484

    def test_custom_values(self):
        config = GlobalConfig(
            host_data_dir="/data",
            port=9000,
            api_tokens="abc,def",
        )
        assert config.host_data_dir == "/data"
        assert config.port == 9000
        assert config.api_tokens == "abc,def"


class TestProjectConfig:
    def test_defaults(self):
        p = ProjectConfig(name="test", repo="https://github.com/a/b")
        assert p.ref == "main"
        assert p.timeout_minutes == 30
        assert p.github_labels.trigger == "wowkmang"

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
        assert len(projects) == 2


class TestFindProjectByRepo:
    def test_find_existing(self, tmp_projects_dir):
        projects = load_projects(tmp_projects_dir)
        result = find_project_by_repo("user/testproject", projects)
        assert result is not None
        assert result.name == "testproject"

    def test_find_missing(self, tmp_projects_dir):
        projects = load_projects(tmp_projects_dir)
        assert find_project_by_repo("unknown/repo", projects) is None

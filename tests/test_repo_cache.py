from unittest.mock import MagicMock

from wowkmang.docker_runner import ContainerResult
from wowkmang.repo_cache import RepoCache


def _mock_docker_runner(exit_code: int = 0, logs: str = ""):
    runner = MagicMock()
    runner.run_git.return_value = ContainerResult(exit_code=exit_code, logs=logs)
    return runner


class TestCacheSubdir:
    def test_https_url(self):
        result = RepoCache.cache_subdir("https://github.com/user/project")
        assert result == "github.com_user_project"

    def test_https_url_with_git_suffix(self):
        result = RepoCache.cache_subdir("https://github.com/user/project.git")
        assert result == "github.com_user_project"

    def test_nested_path(self):
        result = RepoCache.cache_subdir("https://gitlab.com/org/group/project")
        assert result == "gitlab.com_org_group_project"


class TestAuthedUrl:
    def test_no_token_returns_original(self):
        assert (
            RepoCache._authed_url("https://github.com/u/p", None)
            == "https://github.com/u/p"
        )

    def test_empty_token_returns_original(self):
        assert (
            RepoCache._authed_url("https://github.com/u/p", "")
            == "https://github.com/u/p"
        )

    def test_injects_token(self):
        result = RepoCache._authed_url("https://github.com/user/project", "ghp_abc123")
        assert result == "https://x-access-token:ghp_abc123@github.com/user/project"

    def test_preserves_path_and_suffix(self):
        result = RepoCache._authed_url("https://github.com/user/project.git", "tok")
        assert result == "https://x-access-token:tok@github.com/user/project.git"


class TestPrepareWorkdir:
    def test_calls_run_git_with_correct_script(self):
        docker_runner = _mock_docker_runner()
        cache = RepoCache(docker_runner)

        branch = cache.prepare_workdir(
            "https://github.com/user/project",
            "main",
            "work-vol-123",
            "ghcr.io/org/image:latest",
            github_token="ghp_tok",
        )

        assert branch.startswith("wowkmang/")
        assert len(branch) == len("wowkmang/") + 8

        docker_runner.run_git.assert_called_once()
        call_kwargs = docker_runner.run_git.call_args.kwargs
        assert call_kwargs["image"] == "ghcr.io/org/image:latest"
        assert call_kwargs["work_volume"] == "work-vol-123"
        assert call_kwargs["environment"] == {"GIT_TERMINAL_PROMPT": "0"}

        script = call_kwargs["command"]
        assert "github.com_user_project" in script
        assert "x-access-token:ghp_tok@github.com" in script
        assert f"git checkout -b {branch}" in script

    def test_no_token_uses_plain_url(self):
        docker_runner = _mock_docker_runner()
        cache = RepoCache(docker_runner)

        cache.prepare_workdir(
            "https://github.com/user/project",
            "main",
            "vol",
            "img",
        )

        script = docker_runner.run_git.call_args.kwargs["command"]
        assert "x-access-token" not in script
        assert "https://github.com/user/project" in script

    def test_nonzero_exit_raises(self):
        docker_runner = _mock_docker_runner(exit_code=1, logs="fatal: repo not found")
        cache = RepoCache(docker_runner)

        try:
            cache.prepare_workdir(
                "https://github.com/user/project",
                "main",
                "vol",
                "img",
            )
            assert False, "Should have raised"
        except RuntimeError as e:
            assert "repo not found" in str(e)

    def test_script_contains_cache_logic(self):
        docker_runner = _mock_docker_runner()
        cache = RepoCache(docker_runner)

        cache.prepare_workdir(
            "https://github.com/user/project",
            "develop",
            "vol",
            "img",
        )

        script = docker_runner.run_git.call_args.kwargs["command"]
        assert "if [ -d /cache/" in script
        assert "git clone --bare" in script
        assert "git clone --reference" in script
        assert "origin/develop" in script

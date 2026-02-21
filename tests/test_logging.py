import asyncio
import logging
from unittest.mock import MagicMock, patch

from wowkmang.config import GlobalConfig


class TestLogLevelConfig:
    def test_default_log_level_is_info(self):
        config = GlobalConfig()
        assert config.log_level == "info"

    def test_log_level_resolves_to_logging_constant(self):
        config = GlobalConfig(log_level="debug")
        assert getattr(logging, config.log_level.upper()) == logging.DEBUG

    def test_log_level_case_insensitive(self):
        config = GlobalConfig(log_level="WARNING")
        assert getattr(logging, config.log_level.upper()) == logging.WARNING


class TestLoggingSetup:
    def _run_lifespan(self, log_level="debug"):
        with (
            patch("wowkmang.api.config", GlobalConfig(log_level=log_level)),
            patch("wowkmang.api.ensure_queue_dirs"),
            patch("wowkmang.api.load_projects", return_value={}),
            patch("wowkmang.api.Authenticator"),
            patch("wowkmang.api.docker.from_env"),
            patch("wowkmang.api.DockerRunner"),
            patch("wowkmang.api.RepoCache"),
            patch("wowkmang.api.HookRunner"),
            patch("wowkmang.api.FixLoop"),
            patch("wowkmang.api.SummaryGenerator"),
            patch("wowkmang.api.Worker") as MockWorker,
        ):
            MockWorker.return_value = MagicMock()

            from wowkmang.api import lifespan

            async def run():
                async with lifespan(MagicMock()):
                    pass

            asyncio.run(run())

    def test_lifespan_sets_root_logger_level(self):
        self._run_lifespan(log_level="debug")
        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_lifespan_sets_formatter_on_existing_handlers(self):
        self._run_lifespan(log_level="info")
        root = logging.getLogger()
        for handler in root.handlers:
            fmt = handler.formatter._fmt if handler.formatter else ""
            assert "%(asctime)s" in fmt
            assert "%(name)s" in fmt

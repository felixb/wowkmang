import asyncio
import logging
from unittest.mock import MagicMock, patch

from wowkmang.api.config import GlobalConfig


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
            patch("wowkmang.api.routes.config", GlobalConfig(log_level=log_level)),
            patch("wowkmang.api.routes.ensure_queue_dirs"),
            patch("wowkmang.api.routes.load_projects", return_value={}),
            patch("wowkmang.api.routes.Authenticator"),
            patch("wowkmang.api.routes.docker.from_env"),
            patch("wowkmang.api.routes.DockerRunner"),
            patch("wowkmang.api.routes.RepoCache"),
            patch("wowkmang.api.routes.HookRunner"),
            patch("wowkmang.api.routes.FixLoop"),
            patch("wowkmang.api.routes.Worker") as MockWorker,
        ):
            MockWorker.return_value = MagicMock()

            from wowkmang.api.routes import lifespan

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

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config

from openhands.app_server.app_lifespan.app_lifespan_service import AppLifespanService
from openhands.app_server.services.litellm_proxy_manager import (
    LangfuseLiteLLMIntegration,
    configure_openhands_for_proxy,
)
from openhands.app_server.utils.logger import openhands_logger as logger


class OssAppLifespanService(AppLifespanService):
    run_alembic_on_startup: bool = True
    _litellm_integration: LangfuseLiteLLMIntegration | None = None

    async def __aenter__(self):
        if self.run_alembic_on_startup:
            self.run_alembic()
        self._start_litellm_proxy()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self._stop_litellm_proxy()

    # ------------------------------------------------------------------
    # LiteLLM Proxy lifecycle
    # ------------------------------------------------------------------

    def _start_litellm_proxy(self) -> None:
        """Start the LiteLLM Proxy subprocess configured for Langfuse
        observability and route OpenHands traffic through it."""
        try:
            integration = LangfuseLiteLLMIntegration()

            extra_models = [
                {
                    'model_name': 'deepseek-v4-flash-free',
                    'litellm_params': {
                        'model': 'openai/deepseek-v4-flash-free',
                        'api_key': 'sk-l0g79haCLaZaGXpwJ6rwWautjihHrGyngLXhgkQoPYXX93DKXzfUNwza2TFEW5xs',
                        'api_base': 'https://opencode.ai/zen/v1',
                    },
                },
            ]

            success = integration.start(extra_models=extra_models, timeout=30.0)
            if success:
                configure_openhands_for_proxy(integration.proxy_url)
                self._litellm_integration = integration
                logger.info(
                    '[LITELLM-LANGFUSE] Langfuse LiteLLM integration started '
                    'successfully on %s',
                    integration.proxy_url,
                )
            else:
                logger.error(
                    '[LITELLM-LANGFUSE] LiteLLM proxy failed to start — '
                    'LLM traffic will NOT be traced to Langfuse'
                )
        except Exception as exc:
            logger.error(
                '[LITELLM-LANGFUSE] Failed to start Langfuse LiteLLM integration: %s',
                exc,
            )

    def _stop_litellm_proxy(self) -> None:
        """Stop the LiteLLM Proxy subprocess if it was started."""
        integration = self._litellm_integration
        if integration is not None:
            try:
                integration.stop()
                logger.info('[LITELLM-LANGFUSE] Langfuse LiteLLM integration stopped')
            except Exception as exc:
                logger.error(
                    '[LITELLM-LANGFUSE] Error stopping LiteLLM proxy: %s', exc
                )

    def run_alembic(self):
        # Run alembic upgrade head to ensure database is up to date
        alembic_dir = Path(__file__).parent / 'alembic'
        alembic_ini = alembic_dir / 'alembic.ini'

        # Create alembic config with absolute paths
        alembic_cfg = Config(str(alembic_ini))
        alembic_cfg.set_main_option('script_location', str(alembic_dir))

        # Change to alembic directory for the command execution
        original_cwd = os.getcwd()
        try:
            os.chdir(str(alembic_dir.parent))
            command.upgrade(alembic_cfg, 'head')
        finally:
            os.chdir(original_cwd)

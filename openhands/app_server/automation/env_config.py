"""Environment-based LLM configuration for automation restore flow.

When the app-server has no ``settings.json`` (user configured LLM purely via
environment variables), this module provides a fallback that constructs the
``agent_settings`` dict that the sandbox's POST /api/conversations endpoint
needs to resume a conversation with a working LLM.
"""

from __future__ import annotations

import os
import logging

_logger = logging.getLogger(__name__)


# Priority-ordered list of env-var names to check for each LLM field.
# The first non-empty value wins.
#
# The OH_JIRA_ANALYSIS_* vars are listed first because they are the ones
# explicitly set by the user for automation/agent LLM configuration.
# OH_LLM_* / LLM_* are generic fallbacks.
_MODEL_ENV_VARS = (
    'OH_JIRA_ANALYSIS_MODEL',
    'OH_LLM_MODEL',
    'LLM_MODEL',
)
_API_KEY_ENV_VARS = (
    'OH_JIRA_ANALYSIS_API_KEY',
    'OH_LLM_API_KEY',
    'LLM_API_KEY',
)
_BASE_URL_ENV_VARS = (
    'OH_JIRA_ANALYSIS_BASE_URL',
    'OH_LLM_BASE_URL',
    'LLM_BASE_URL',
)


def _first_non_empty(*names: str) -> str | None:
    """Return the value of the first env var in *names that is set and non-empty."""
    for name in names:
        val = os.environ.get(name)
        if val and val.strip():
            return val.strip()
    return None


def build_agent_settings_from_env() -> dict:
    """Build a minimal ``agent_settings`` dict from environment variables.

    Checks (in priority order):
      ``OH_JIRA_ANALYSIS_MODEL`` / ``OH_JIRA_ANALYSIS_API_KEY`` / ``OH_JIRA_ANALYSIS_BASE_URL``
      ``OH_LLM_MODEL`` / ``OH_LLM_API_KEY`` / ``OH_LLM_BASE_URL``
      ``LLM_MODEL`` / ``LLM_API_KEY`` / ``LLM_BASE_URL``

    Returns a dict with structure suitable for ``POST /api/conversations``
    ``agent_settings`` field:

    .. code-block:: python

        {
            'llm': {
                'model': '...',
                'api_key': '...',
                'base_url': '...',
            },
        }

    Any field that cannot be resolved from the environment is omitted so
    the sandbox's own defaults apply.
    """
    model = _first_non_empty(*_MODEL_ENV_VARS)
    api_key = _first_non_empty(*_API_KEY_ENV_VARS)
    base_url = _first_non_empty(*_BASE_URL_ENV_VARS)

    llm: dict[str, str] = {}
    if model:
        llm['model'] = model
    if api_key:
        llm['api_key'] = api_key
    if base_url:
        llm['base_url'] = base_url

    if not llm:
        _logger.warning(
            '[EnvConfig] No LLM configuration found in environment vars %s, %s, %s. '
            'The restored agent will lack an LLM.',
            '/'.join(_MODEL_ENV_VARS),
            '/'.join(_API_KEY_ENV_VARS),
            '/'.join(_BASE_URL_ENV_VARS),
        )
        return {}

    _logger.info(
        '[EnvConfig] Built agent_settings from env vars (model=%s, base_url=%s)',
        model,
        base_url,
    )
    return {'llm': llm}

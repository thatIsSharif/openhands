"""Prompt renderer for automation services.

Centralizes Jinja2 template loading and rendering for all automation
conversation prompts. Follows the same pattern used by enterprise
integration managers (see enterprise/integrations/*Manager.py).

Templates are loaded from:
    openhands/app_server/integrations/templates/resolver/automation/
"""

from __future__ import annotations

from jinja2 import Environment, FileSystemLoader

from .constants import AUTOMATION_TEMPLATES_DIR as _AUTOMATION_TEMPLATES_DIR

_jinja_env: Environment | None = None


def _get_env() -> Environment:
    """Get or create the shared Jinja2 environment."""
    global _jinja_env
    if _jinja_env is None:
        _jinja_env = Environment(
            loader=FileSystemLoader(_AUTOMATION_TEMPLATES_DIR),
            autoescape=False,
        )
    return _jinja_env


def render_prompt(template_name: str, **kwargs) -> str:
    """Render an automation conversation prompt from a Jinja2 template.

    Args:
        template_name: Name of the template file (e.g. ``jira_new_conversation.j2``).
        **kwargs: Template variables.

    Returns:
        The rendered prompt string.
    """
    env = _get_env()
    template = env.get_template(template_name)
    return template.render(**kwargs)

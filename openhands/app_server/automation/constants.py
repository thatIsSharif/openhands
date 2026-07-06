"""Shared constants for the automation platform."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

AUTOMATION_TEMPLATES_DIR = (
    'openhands/app_server/integrations/templates/resolver/automation'
)

# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

REJECTION_MESSAGE = (
    '🚨 **Security Alert**: Your input was rejected by Layer 1 security '
    'because it contains potentially dangerous patterns (prompt injection, '
    'jailbreak attempts, or dangerous commands).\n\n'
    'This conversation will not be processed. Please remove any suspicious '
    'content and try again.'
)

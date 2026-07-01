"""Input sanitization for automation platform prompts.

Layer 1 security: sanitize user-controlled text fields before they are
rendered into Jinja2 templates for LLM prompts. Prevents prompt injection,
template injection, and jailbreak attempts.

Applied to ALL 4 automation entry points:
1. JIRA issue_created → jira_new_conversation.j2
2. JIRA @openhands comment → jira_existing_conversation.j2
3. GitHub review comment → github_review_conversation.j2
4. GitHub review submitted → github_review_submitted_conversation.j2 / existing
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Identifier validation
# ---------------------------------------------------------------------------

_JIRA_ISSUE_KEY_RE = re.compile(r'^[A-Z][A-Z0-9_]{1,49}-\d{1,10}$')


def validate_jira_issue_key(key: str) -> bool:
    """Validate a JIRA issue key format (e.g. PROJ-123).

    Must match: 2-50 uppercase letters/digits, hyphen, 1-10 digits.
    """
    return bool(_JIRA_ISSUE_KEY_RE.match(key))


def validate_github_pr_number(number: int) -> bool:
    """Validate a GitHub PR/issue number is a reasonable positive integer."""
    return isinstance(number, int) and 1 <= number <= 10_000_000


# ---------------------------------------------------------------------------
# Injection patterns — stripped from all text fields before template rendering
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # "Ignore all previous instructions" and variants
    (
        re.compile(
            r'\b(?:ignore|disregard|forget|override|bypass|skip)\s+'
            r'(?:all\s+)?(?:previous|prior|above|former)\s+'
            r'(?:instructions?|directives?|prompts?|commands?|rules?|orders?)\b',
            re.IGNORECASE,
        ),
        'instruction_override',
    ),
    # Template injection — Jinja2/ERB/Mustache
    (
        re.compile(r'\{\{.*?\}\}|\{%.*?%\}', re.IGNORECASE),
        'template_injection',
    ),
    # "Ignore" block markers
    (
        re.compile(r'```\s*(?:ignore|system|prompt|assistant)\s*\n', re.IGNORECASE),
        'block_marker',
    ),
    # Jailbreak: "You are DAN", "You are now in developer mode", etc.
    (
        re.compile(
            r'\byou\s+are\s+(?:now\s+|currently\s+)?'
            r'(?:dan|developer\s+mode|free\s+(?:mode|from\s+constraints?)|'
            r'unconstrained|unlimited|god\s+mode)\b',
            re.IGNORECASE,
        ),
        'jailbreak',
    ),
    # "Pretend" / role-play escapes
    (
        re.compile(
            r'\bpretend\s+(?:you\s+are|to\s+be)\s+'
            r'(?:a\s+|an\s+)?(?:different|unrestricted|unconstrained|'
            r'new|superior)\s+\w+\b',
            re.IGNORECASE,
        ),
        'roleplay_escape',
    ),
    # Hidden instruction in code blocks — trail after ```
    (
        re.compile(
            r'```[\w]*\n?(?:.+\n)*?'
            r'(?:remember|secretly|actually|but\s+really)'
            r'.+?:',
            re.IGNORECASE,
        ),
        'hidden_instruction',
    ),
    # Base64 encoded payloads (long, high-entropy base64 strings)
    (
        re.compile(
            r'(?:[A-Za-z0-9+/]{40,}(?:[A-Za-z0-9+/=]{2,})?)',
        ),
        'base64_payload',
    ),
    # Multi-stage: "first do X, then when Y happens, do Z"
    (
        re.compile(
            r'\bfirst\s+.{0,50}?\bthen\s+(?:when|after|once)\b'
            r'.{0,100}?\b(?:delete|drop|remove|execute|run|bypass)\b',
            re.IGNORECASE,
        ),
        'multi_stage',
    ),
    # System prompt extraction attempts
    (
        re.compile(
            r'\b(?:print|output|display|reveal|show|leak|echo)\s+'
            r'(?:your\s+)?(?:system\s+)?(?:prompt|instructions?|rules?)',
            re.IGNORECASE,
        ),
        'prompt_extraction',
    ),
    # Reset context attempts
    (
        re.compile(
            r'\breset\s+(?:the\s+)?(?:context|conversation|chat|session)\b',
            re.IGNORECASE,
        ),
        'context_reset',
    ),
]

# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------

_SANITIZED_REPLACEMENT = ' [REMOVED] '


def sanitize_input(text: str, field_name: str = 'unknown') -> str:
    """Sanitize input text by stripping injection patterns.

    Scans the text for known injection, jailbreak, and template-injection
    patterns. When a match is found, the matching span is replaced with a
    safe marker and the event is logged.

    Args:
        text: The input text to sanitize.
        field_name: A human-readable label for the field being sanitized
            (used in log messages).

    Returns:
        The sanitized text with injection patterns removed/replaced.
    """
    if not text or not isinstance(text, str):
        return text

    sanitized = text

    for pattern, label in _INJECTION_PATTERNS:
        matches = list(pattern.finditer(sanitized))
        for match in reversed(matches):
            span_start, span_end = match.span()
            sanitized = (
                sanitized[:span_start] + _SANITIZED_REPLACEMENT + sanitized[span_end:]
            )

        if matches:
            logger.warning(
                '[Security] Input sanitization triggered: field=%s pattern=%s '
                'matches=%d',
                field_name,
                label,
                len(matches),
            )

    return sanitized

"""Input sanitization for automation platform prompts.

Layer 1 security: detect dangerous patterns in user-controlled text fields
before they are rendered into Jinja2 templates for LLM prompts. Prevents
prompt injection, template injection, and jailbreak attempts.

When dangerous patterns are detected, the conversation is stopped (not created)
and a security alert comment is posted back to the source (GitHub PR or Jira
issue) using the existing comment-posting API endpoints.

Applied to ALL 4 automation entry points:
1. JIRA issue_created → jira_new_conversation.j2
2. JIRA @openhands comment → jira_existing_conversation.j2
3. GitHub review comment → github_review_conversation.j2
4. GitHub review submitted → github_review_submitted_conversation.j2 / existing
"""

from __future__ import annotations

import logging
import pathlib
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Identifier validation
# ---------------------------------------------------------------------------

_JIRA_ISSUE_KEY_RE = re.compile(r'^[A-Z][A-Z0-9_]{1,49}-\d{1,10}$')


def validate_jira_issue_key(key: str | None) -> bool:
    """Validate a JIRA issue key format (e.g. PROJ-123).

    Must match: 2-50 uppercase letters/digits, hyphen, 1-10 digits.
    Returns False for None or invalid format.
    """
    if not isinstance(key, str):
        return False
    return bool(_JIRA_ISSUE_KEY_RE.match(key))


def validate_github_pr_number(number: int) -> bool:
    """Validate a GitHub PR/issue number is a reasonable positive integer."""
    return isinstance(number, int) and 1 <= number <= 10_000_000


# ---------------------------------------------------------------------------
# Injection patterns — stripped from all text fields before template rendering
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # ── Role-play / jailbreak escapes ────────────────────────────
    # "Pretend you are" / "Act as if" / "You are now" role-play
    (
        re.compile(
            r'\b(?:pretend|act)\s+(?:that\s+)?(?:you\s+)?(?:are|as)\b',
            re.IGNORECASE,
        ),
        'roleplay_escape',
    ),
    # "Developer mode" / "DAN" jailbreak
    (
        re.compile(
            r'\b(?:developer\s+mode|(?:you\s+(?:are\s+)?)?DAN(?:iel)?)\b',
            re.IGNORECASE,
        ),
        'jailbreak_dan',
    ),
    # "New conversation" / "New chat" / "fresh context" context reset
    (
        re.compile(
            r'\b(?:new|fresh)\s+(?:conversation|chat|context|session)\b',
            re.IGNORECASE,
        ),
        'context_reset_new',
    ),
    # "Forget the above" / "Disregard previous" / "Ignore everything above"
    (
        re.compile(
            r'\b(?:forget|disregard|ignore|skip)\s+(?:the\s+)?'
            r'(?:above|previous|everything\s+(?:above|before|so\s+far))\b',
            re.IGNORECASE,
        ),
        'instruction_override_context',
    ),
    # "Step N: ..." multi-stage instruction attack
    (
        re.compile(
            r'\b(?:step|phase|stage)\s+\d+\s*[:\-–—].{0,200}?\b'
            r'(?:step|phase|stage)\s+\d+\s*[:\-–—]',
            re.IGNORECASE | re.DOTALL,
        ),
        'multi_step_attack',
    ),
    # Base64-encoded payloads (40+ chars of base64)
    (
        re.compile(
            r'(?:[A-Za-z0-9+/]{40,}(?:=|==)?)',
            re.IGNORECASE,
        ),
        'base64_payload',
    ),
    # ── Dangerous embedded commands ──────────────────────────────
    # git push --force variants (direct push to main with force)
    (
        re.compile(
            r'\bgit\s+push\s+.*?(?:--force|-f|main|master).*?(?:--force|-f)?\b',
            re.IGNORECASE,
        ),
        'dangerous_git_push',
    ),
    # git push origin HEAD:main (direct branch override)
    (
        re.compile(
            r'\bgit\s+push\s+origin\s+HEAD\s*:\s*(?:main|master)\b',
            re.IGNORECASE,
        ),
        'dangerous_git_push_direct',
    ),
    # git checkout main/master (then destructive operations)
    (
        re.compile(
            r'\bgit\s+checkout\s+(?:main|master)\b',
            re.IGNORECASE,
        ),
        'dangerous_git_checkout_main',
    ),
    # git commit -a / git commit directly to main
    (
        re.compile(
            r'\bgit\s+commit\b.*?(?:main|master)',
            re.IGNORECASE,
        ),
        'dangerous_git_commit_main',
    ),
    # git reset --hard
    (
        re.compile(r'\bgit\s+reset\s+--hard\b', re.IGNORECASE),
        'dangerous_git_reset_hard',
    ),
    # git merge main/master
    (
        re.compile(r'\bgit\s+merge\s+(?:main|master)\b', re.IGNORECASE),
        'dangerous_git_merge_main',
    ),
    # rm -rf on root or system dirs
    (
        re.compile(
            r'\brm\s+(?:-[rf]+\s+|\s+).*?(?:\/|\.\s+|\.$|\/etc|\/usr|\/var|\/home|\/root|\/opt)',
            re.IGNORECASE,
        ),
        'dangerous_rm_rf',
    ),
    # DROP DATABASE / TABLE
    (
        re.compile(r'\bDROP\s+(?:DATABASE|TABLE|SCHEMA)\b', re.IGNORECASE),
        'dangerous_drop_db',
    ),
    # DELETE FROM without WHERE
    (
        re.compile(r'\bDELETE\s+FROM\b(?!.*\bWHERE\b)', re.IGNORECASE),
        'dangerous_delete_no_where',
    ),
    # TRUNCATE
    (
        re.compile(r'\bTRUNCATE\b', re.IGNORECASE),
        'dangerous_truncate',
    ),
    # mkfs / dd (disk operations)
    (
        re.compile(r'\bmkfs\b', re.IGNORECASE),
        'dangerous_mkfs',
    ),
    (
        re.compile(r'\bdd\b.{0,50}(?:of=|if=)', re.IGNORECASE),
        'dangerous_dd',
    ),
    # chmod -R 000 (remove all permissions)
    (
        re.compile(r'\bchmod\s+-R\s+0{3,4}\b', re.IGNORECASE),
        'dangerous_chmod',
    ),
    # find / -delete (mass deletion)
    (
        re.compile(r'\bfind\s+/\s+-type\s+[fd]\s+-delete\b', re.IGNORECASE),
        'dangerous_find_delete',
    ),
    # ── Original injection patterns ─────────────────────────────
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

REJECTION_MESSAGE = (
    '🚨 **Security Alert**: Your input was rejected by Layer 1 security '
    'because it contains potentially dangerous patterns (prompt injection, '
    'jailbreak attempts, or dangerous commands).\n\n'
    'This conversation will not be processed. Please remove any suspicious '
    'content and try again.'
)

# Layer 2 command enforcement delegates to ``block_dangerous.sh`` — the
# same PreToolUse hook script that blocks commands at the runtime level
# before execution. The ``CommandEnforcementProcessor`` handles the
# post-hoc notification layer (posting a comment to GitHub/Jira and
# stopping the conversation).

_BLOCK_DANGEROUS_SH_PATH = (
    pathlib.Path(__file__).resolve().parent.parent.parent.parent
    / '.openhands'
    / 'hooks'
    / 'block_dangerous.sh'
)


def has_dangerous_command(command: str) -> tuple[bool, str | None]:
    """Check if a shell command contains dangerous operations, using
    ``block_dangerous.sh``.

    Layer 2 detection: invokes the same ``block_dangerous.sh`` script
    that runs as a PreToolUse hook in the sandbox, passing the command
    in the expected JSON format on stdin. If the script exits with
    code 2, the command is considered dangerous.

    Args:
        command: The shell command string to check.

    Returns:
        A tuple of ``(is_dangerous, matched_label)`` where ``is_dangerous``
        is ``True`` if a dangerous pattern was found, and ``matched_label``
        is a short description or ``None``.
    """
    if not command or not isinstance(command, str):
        return False, None

    import json
    import subprocess

    script_path = str(_BLOCK_DANGEROUS_SH_PATH)

    if not pathlib.Path(script_path).exists():
        logger.warning(
            '[Security] block_dangerous.sh not found at %s '
            '(Layer 2 check skipped)',
            script_path,
        )
        return False, None

    stdin_payload = json.dumps({
        'event_type': 'PreToolUse',
        'tool_name': 'terminal',
        'tool_input': {'command': command},
    })

    env = {
        'OPENHANDS_EVENT_TYPE': 'PreToolUse',
        'OPENHANDS_TOOL_NAME': 'terminal',
    }

    try:
        result = subprocess.run(
            ['bash', script_path],
            input=stdin_payload,
            capture_output=True,
            timeout=10,
            text=True,
            env=env,
        )

        if result.returncode == 2:
            # Script outputs JSON with a "reason" field when denying
            reason = ''
            try:
                reason = json.loads(result.stdout).get('reason', '')
            except (json.JSONDecodeError, ValueError):
                reason = result.stderr.strip() or 'dangerous command blocked'

            logger.warning(
                '[Security] Dangerous command detected (Layer 2): '
                'reason=%s command=%r',
                reason,
                command[:200],
            )
            return True, reason or 'dangerous_command'
        elif result.returncode == 1:
            logger.warning(
                '[Security] block_dangerous.sh returned error code 1 '
                '(non-blocking) for command=%r: stderr=%s',
                command[:200],
                result.stderr.strip(),
            )

        return False, None

    except FileNotFoundError:
        logger.warning(
            '[Security] Could not execute block_dangerous.sh '
            '(bash not found, Layer 2 check skipped)'
        )
        return False, None
    except subprocess.TimeoutExpired:
        logger.warning(
            '[Security] block_dangerous.sh timed out for command=%r '
            '(Layer 2 check skipped)',
            command[:200],
        )
        return False, None


def sanitize_input(text: str, field_name: str = 'unknown') -> str:
    """Sanitize input text by stripping injection patterns.

    Scans the text for known injection, jailbreak, and template-injection
    patterns. When a match is found, the matching span is replaced with a
    safe marker and the event is logged.

    Note: This function is kept for backward compatibility. New code should
    prefer ``has_dangerous_patterns()`` to check input and reject early.

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


def has_dangerous_patterns(
    text: str, field_name: str = 'unknown'
) -> tuple[bool, list[str]]:
    """Check if text contains dangerous injection/jailbreak patterns.

    Scans the text for known injection, jailbreak, and template-injection
    patterns. Intended to be called before processing input — if dangerous
    patterns are found the caller should reject the input, post a security
    alert comment back to the source (GitHub PR / Jira issue), and stop
    processing.

    Args:
        text: The input text to check.
        field_name: A human-readable label for the field being checked
            (used in log messages).

    Returns:
        A tuple of ``(has_danger, matched_labels)`` where ``has_danger`` is
        ``True`` if dangerous patterns were found, and ``matched_labels`` is
        a list of pattern labels that matched (e.g. ``['jailbreak_dan',
        'instruction_override']``).
    """
    if not text or not isinstance(text, str):
        return False, []

    matched_labels: list[str] = []
    for pattern, label in _INJECTION_PATTERNS:
        if pattern.search(text):
            matched_labels.append(label)
            logger.warning(
                '[Security] Dangerous pattern detected: field=%s pattern=%s',
                field_name,
                label,
            )

    if matched_labels:
        logger.warning(
            '[Security] Input blocked due to dangerous patterns: '
            'field=%s patterns=%s',
            field_name,
            matched_labels,
        )

    return len(matched_labels) > 0, matched_labels

"""Automation Security Analyzer — guardrails for agent actions in automations.

Layer 2 security: a custom SecurityAnalyzer that blocks dangerous commands
and operations in automation-triggered conversations (JIRA / GitHub).

Designed to be combined with the SDK's PatternSecurityAnalyzer and
PolicyRailSecurityAnalyzer in an EnsembleSecurityAnalyzer.

Detects:
- Destructive filesystem and database commands
- Production resource access
- Dangerous git operations
- Dangerous GitHub API operations
- Large-scale code deletion
"""

from __future__ import annotations

import re

from openhands.sdk.event import ActionEvent
from openhands.sdk.logger import get_logger
from openhands.sdk.security.analyzer import SecurityAnalyzerBase
from openhands.sdk.security.risk import SecurityRisk

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Stable detector IDs
# ---------------------------------------------------------------------------

DET_DB_DROP_DATABASE = 'auto.db.drop_database'
DET_DB_DROP_TABLE = 'auto.db.drop_table'
DET_DB_DELETE_NO_WHERE = 'auto.db.delete_no_where'
DET_DB_TRUNCATE = 'auto.db.truncate'
DET_GIT_PUSH_FORCE = 'auto.git.push_force'
DET_GIT_PUSH_MAIN = 'auto.git.push_main'
DET_GIT_COMMIT_MAIN = 'auto.git.commit_main'
DET_GIT_RESET_HARD = 'auto.git.reset_hard'
DET_GIT_DELETE_BRANCH = 'auto.git.delete_branch'
DET_GIT_MERGE_MAIN = 'auto.git.merge_main'
DET_PROD_RESOURCE = 'auto.prod.resource_access'
DET_PROD_DB = 'auto.prod.database'
DET_PROD_SERVER = 'auto.prod.server'
DET_PROD_BRANCH = 'auto.prod.branch'
DET_FS_MASS_DELETE = 'auto.fs.mass_delete'
DET_FS_DESTRUCTIVE = 'auto.fs.destructive'
DET_GITHUB_DELETE_REPO = 'auto.github.delete_repo'
DET_GITHUB_PROTECTED_BRANCH = 'auto.github.protected_branch'
DET_GITHUB_REPO_SETTINGS = 'auto.github.repo_settings'

# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# HIGH: Destructive database commands
HIGH_DB_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r'\bDROP\s+(?:DATABASE|SCHEMA)\b', re.IGNORECASE),
        'Drop database or schema',
        DET_DB_DROP_DATABASE,
    ),
    (
        re.compile(r'\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?', re.IGNORECASE),
        'Drop table',
        DET_DB_DROP_TABLE,
    ),
    (
        re.compile(
            r'\bDELETE\s+FROM\b(?!.*\bWHERE\b)',
            re.IGNORECASE | re.DOTALL,
        ),
        'DELETE FROM without WHERE clause',
        DET_DB_DELETE_NO_WHERE,
    ),
    (
        re.compile(r'\bTRUNCATE\b', re.IGNORECASE),
        'TRUNCATE table',
        DET_DB_TRUNCATE,
    ),
]

# HIGH: Dangerous git operations (executable fields only)
HIGH_GIT_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(
            r'\bgit\s+push\s+(?:-[^\s]*f[^\s]*\s+|.*?\s+)?'
            r'(?:--force-with-lease|--force|-f)\b',
            re.IGNORECASE,
        ),
        'Git push --force (any branch)',
        DET_GIT_PUSH_FORCE,
    ),
    (
        re.compile(
            r'\bgit\s+push\b(?!.*\s(?:--force|-f))\s+'
            r'(?:\w+\s+)?(?:main|master)\s*(?:\s|$)',
            re.IGNORECASE,
        ),
        'Git push to main/master',
        DET_GIT_PUSH_MAIN,
    ),
    (
        re.compile(
            r'\bgit\s+commit\s+.*?\s+(?:main|master)(?:\s|$)',
            re.IGNORECASE,
        ),
        'Git commit directly to main/master',
        DET_GIT_COMMIT_MAIN,
    ),
    (
        re.compile(r'\bgit\s+reset\s+--hard\b', re.IGNORECASE),
        'Git reset --hard',
        DET_GIT_RESET_HARD,
    ),
    (
        re.compile(
            r'\bgit\s+branch\s+-[dD]\s+(?:main|master|production|live|primary)\b',
            re.IGNORECASE,
        ),
        'Delete production branch',
        DET_GIT_DELETE_BRANCH,
    ),
    (
        re.compile(
            r'\bgit\s+merge\s+(?:main|master)\b',
            re.IGNORECASE,
        ),
        'Merge main/master into current branch',
        DET_GIT_MERGE_MAIN,
    ),
]

# HIGH: Production resource access
HIGH_PROD_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(
            r'\b(?:prod|production|live|primary)'
            r'(?:_|\b)'
            r'(?:db|database|server|host|cluster|instance|conn|url|endpoint)'
            r'(?:_|\b)?',
            re.IGNORECASE,
        ),
        'Production database/server access',
        DET_PROD_RESOURCE,
    ),
    (
        re.compile(
            r'\b(?:psql|mysql|mariadb|pg_isready|mongosh|redis-cli|sqlplus)\s+'
            r'.{0,100}'
            r'(?:prod|production|live)',
            re.IGNORECASE,
        ),
        'Production database connection',
        DET_PROD_DB,
    ),
    (
        re.compile(
            r'\b(?:ssh|kubectl|helm)\s+.{0,100}'
            r'(?:prod|production|live)',
            re.IGNORECASE,
        ),
        'Production server access',
        DET_PROD_SERVER,
    ),
    (
        re.compile(
            r'\b(?:gh|git)\s+checkout\s+(?:prod|production|live|primary)\b',
            re.IGNORECASE,
        ),
        'Checkout production branch',
        DET_PROD_BRANCH,
    ),
]

# HIGH: Filesystem destructive commands
HIGH_FS_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(
            r'\brm\s+-(?:[rR]|[fF])\s+.*?'
            r'(?:/\s*$|/\s+\||/\w{0,20}\s+\||/etc|/usr|/var|/home|/root|/opt)',
            re.IGNORECASE,
        ),
        'Recursive delete targeting system directories',
        DET_FS_MASS_DELETE,
    ),
    (
        re.compile(
            r'\bfind\s+/\s+-type\s+[fd]\s+-delete\b',
            re.IGNORECASE,
        ),
        'Mass filesystem deletion via find',
        DET_FS_MASS_DELETE,
    ),
    (
        re.compile(
            r'\bchmod\s+-R\s+0{3,4}\b',
            re.IGNORECASE,
        ),
        'Remove all permissions recursively',
        DET_FS_DESTRUCTIVE,
    ),
    (
        re.compile(
            r'\bmkfs\b',
            re.IGNORECASE,
        ),
        'Filesystem format',
        DET_FS_DESTRUCTIVE,
    ),
    (
        re.compile(
            r'\bdd\b',
            re.IGNORECASE,
        ),
        'Raw disk operation',
        DET_FS_DESTRUCTIVE,
    ),
]

# HIGH: Dangerous GitHub CLI operations
HIGH_GITHUB_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(
            r'\bgh\s+repo\s+delete\b',
            re.IGNORECASE,
        ),
        'Delete GitHub repository',
        DET_GITHUB_DELETE_REPO,
    ),
    (
        re.compile(
            r'\bgh\s+api\s+.*?'
            r'(?:remove-protected-branch|required_status_checks|'
            r'dismiss_stale_reviews|require_code_owner_reviews)',
            re.IGNORECASE,
        ),
        'Modify protected branch settings',
        DET_GITHUB_PROTECTED_BRANCH,
    ),
    (
        re.compile(
            r'\bgh\s+api\s+.*?'
            r'(?:/repos/.*?/branches/.*?/protection)',
            re.IGNORECASE,
        ),
        'Branch protection API access',
        DET_GITHUB_PROTECTED_BRANCH,
    ),
    (
        re.compile(
            r'\bgh\s+api\s+.*?'
            r'(?:/repos/.*?/?(?:actions|pages|topics|traffic|transfer))',
            re.IGNORECASE,
        ),
        'Repository settings modification',
        DET_GITHUB_REPO_SETTINGS,
    ),
]


# ---------------------------------------------------------------------------
# AutomationSecurityAnalyzer
# ---------------------------------------------------------------------------


class AutomationSecurityAnalyzer(SecurityAnalyzerBase):
    """Security analyzer for automation-specific threats.

    Catches dangerous operations that are especially risky in the context
    of automated JIRA/GitHub workflows: destructive database commands,
    production resource access, dangerous git operations, and large-scale
    file deletion.

    Designed to be composed with the SDK's ``PatternSecurityAnalyzer``
    and ``PolicyRailSecurityAnalyzer`` in an ``EnsembleSecurityAnalyzer``.

    Example::

        from openhands.sdk.security import EnsembleSecurityAnalyzer
        from openhands.sdk.security.defense_in_depth import (
            PatternSecurityAnalyzer,
            PolicyRailSecurityAnalyzer,
        )
        from openhands.app_server.automation.automation_security_analyzer import (
            AutomationSecurityAnalyzer,
        )

        analyzer = EnsembleSecurityAnalyzer(
            analyzers=[
                PolicyRailSecurityAnalyzer(),
                PatternSecurityAnalyzer(),
                AutomationSecurityAnalyzer(),
            ]
        )
    """

    def security_risk(self, action: ActionEvent) -> SecurityRisk:
        """Evaluate security risk for automation-specific threats.

        Scans the action's tool call arguments (the executable content)
        for patterns that indicate destructive or dangerous operations
        in the automation context.

        Returns:
            SecurityRisk.HIGH if a dangerous pattern is matched,
            SecurityRisk.LOW otherwise.
        """
        # Extract executable content from the action
        exec_content = self._extract_exec_content(action)
        if not exec_content:
            return SecurityRisk.LOW

        # Normalize the content
        normalized = self._normalize(exec_content)

        # Check HIGH patterns first
        for pattern, _desc, det_id in HIGH_DB_PATTERNS:
            if pattern.search(normalized):
                logger.debug(
                    'Automation security match: %s (%s) -> HIGH',
                    det_id,
                    _desc,
                )
                return SecurityRisk.HIGH

        for pattern, _desc, det_id in HIGH_GIT_PATTERNS:
            if pattern.search(normalized):
                logger.debug(
                    'Automation security match: %s (%s) -> HIGH',
                    det_id,
                    _desc,
                )
                return SecurityRisk.HIGH

        for pattern, _desc, det_id in HIGH_PROD_PATTERNS:
            if pattern.search(normalized):
                logger.debug(
                    'Automation security match: %s (%s) -> HIGH',
                    det_id,
                    _desc,
                )
                return SecurityRisk.HIGH

        for pattern, _desc, det_id in HIGH_FS_PATTERNS:
            if pattern.search(normalized):
                logger.debug(
                    'Automation security match: %s (%s) -> HIGH',
                    det_id,
                    _desc,
                )
                return SecurityRisk.HIGH

        for pattern, _desc, det_id in HIGH_GITHUB_PATTERNS:
            if pattern.search(normalized):
                logger.debug(
                    'Automation security match: %s (%s) -> HIGH',
                    det_id,
                    _desc,
                )
                return SecurityRisk.HIGH

        return SecurityRisk.LOW

    @staticmethod
    def _extract_exec_content(action: ActionEvent) -> str:
        """Extract executable content from an ActionEvent for scanning.

        Combines tool_name and tool_call arguments, which represent
        what the agent will actually execute.
        """
        parts: list[str] = []

        if action.tool_name:
            parts.append(action.tool_name)

        if action.tool_call and action.tool_call.arguments:
            parts.append(action.tool_call.arguments)

        return ' '.join(parts)

    @staticmethod
    def _normalize(text: str) -> str:
        """Basic normalization: lowercase, collapse whitespace."""
        import unicodedata

        text = text.replace('\x00', '')
        text = unicodedata.normalize('NFKC', text)
        return ' '.join(text.split())

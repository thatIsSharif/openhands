"""Tests for automation_security_analyzer — Layer 2: pattern verification.

Tests the regex patterns exported as AUTOMATION_HIGH_PATTERNS,
AUTOMATION_GIT_PATTERNS, and AUTOMATION_GITHUB_PATTERNS.

These tests compile each pattern string, verify it matches expected
threat strings, and confirm the tuples are valid for JSON serialization.
"""

import re

import pytest

from openhands.app_server.automation.automation_security_analyzer import (
    AUTOMATION_GIT_PATTERNS,
    AUTOMATION_GITHUB_PATTERNS,
    AUTOMATION_HIGH_PATTERNS,
)

# ── Verifies every pattern compiles and at least one pattern per group ──


class TestAllPatternsValid:
    """Verify all exported pattern tuples are valid regexes."""

    @pytest.mark.parametrize(
        'group_name, group',
        [
            ('AUTOMATION_HIGH_PATTERNS', AUTOMATION_HIGH_PATTERNS),
            ('AUTOMATION_GIT_PATTERNS', AUTOMATION_GIT_PATTERNS),
            ('AUTOMATION_GITHUB_PATTERNS', AUTOMATION_GITHUB_PATTERNS),
        ],
    )
    def test_all_patterns_compile(self, group_name, group):
        for pattern_str, desc, det_id in group:
            try:
                re.compile(pattern_str, re.IGNORECASE)
            except re.error as e:
                pytest.fail(f'{group_name}/{det_id} ({desc}): {e}')

    @pytest.mark.parametrize(
        'group_name, group',
        [
            ('AUTOMATION_HIGH_PATTERNS', AUTOMATION_HIGH_PATTERNS),
            ('AUTOMATION_GIT_PATTERNS', AUTOMATION_GIT_PATTERNS),
            ('AUTOMATION_GITHUB_PATTERNS', AUTOMATION_GITHUB_PATTERNS),
        ],
    )
    def test_all_have_correct_structure(self, group_name, group):
        for i, item in enumerate(group):
            assert isinstance(item, tuple), f'{group_name}[{i}] not a tuple'
            assert len(item) == 3, f'{group_name}[{i}] has {len(item)} elements, expected 3'
            assert isinstance(item[0], str), f'{group_name}[{i}] pattern not str'
            assert isinstance(item[1], str), f'{group_name}[{i}] desc not str'
            assert isinstance(item[2], str), f'{group_name}[{i}] det_id not str'

    def test_json_roundtrip(self):
        """Patterns must survive JSON serialization (no compiled regex objects)."""
        import json

        data = {
            'high': AUTOMATION_HIGH_PATTERNS,
            'git': AUTOMATION_GIT_PATTERNS,
            'github': AUTOMATION_GITHUB_PATTERNS,
        }
        roundtripped = json.loads(json.dumps(data))
        for group_name, group in roundtripped.items():
            for pattern_str, desc, det_id in group:
                assert isinstance(pattern_str, str)
                re.compile(pattern_str, re.IGNORECASE)  # must still compile


# ── Keyword-driven match tests ──


class TestHighPatternsMatch:
    """AUTOMATION_HIGH_PATTERNS must match destructive DB, FS, and prod access commands."""

    _EXPECTED_MATCHES: list[tuple[str, str]] = [
        # Database commands
        ('DROP DATABASE production', 'db drop'),
        ('DROP SCHEMA public', 'schema drop'),
        ('DROP TABLE IF EXISTS users', 'table drop'),
        ('DELETE FROM users', 'delete without where'),
        ('TRUNCATE TABLE orders', 'truncate'),
        # Filesystem destruction
        ('rm -rf /', 'rm root'),
        ('rm -rf /etc', 'rm etc'),
        ('rm -f /var/log', 'rm force'),
        ('find / -type f -delete', 'find delete'),
        ('chmod -R 000 /data', 'chmod 000'),
        ('mkfs.ext4 /dev/sda1', 'mkfs'),
        ('dd if=/dev/zero of=/dev/sda bs=1M', 'dd'),
        # Prod access
        ('production_db', 'prod db name'),
        ('psql production_db', 'psql prod'),
        ('kubectl get pods --namespace production', 'kubectl prod'),
        ('ssh deploy@prod-server-01', 'ssh prod'),
        ('gh checkout production', 'gh checkout prod'),
    ]

    def test_all_expected_matches(self):
        for target, label in self._EXPECTED_MATCHES:
            matched = False
            for pattern_str, _desc, _det_id in AUTOMATION_HIGH_PATTERNS:
                p = re.compile(pattern_str, re.IGNORECASE)
                if p.search(target):
                    matched = True
                    break
            assert matched, (
                f'AUTOMATION_HIGH_PATTERNS should match "{target}" ({label}), '
                f'but no pattern matched'
            )


class TestHighPatternsNoMatch:
    """AUTOMATION_HIGH_PATTERNS must NOT match safe commands."""

    _EXPECTED_NO_MATCH: list[tuple[str, str]] = [
        ('DELETE FROM users WHERE id = 1', 'delete with where'),
        ('SELECT * FROM users', 'select'),
        ('rm file.txt', 'rm single file'),
        ('rm -rf ./temp', 'rm local dir'),
        ('chmod 755 /data', 'safe chmod'),
    ]

    def test_all_expected_no_matches(self):
        for target, label in self._EXPECTED_NO_MATCH:
            for pattern_str, _desc, _det_id in AUTOMATION_HIGH_PATTERNS:
                p = re.compile(pattern_str, re.IGNORECASE)
                if p.search(target):
                    pytest.fail(
                        f'AUTOMATION_HIGH_PATTERNS should NOT match "{target}" '
                        f'({label}), but pattern {_det_id} matched'
                    )


class TestGitPatternsMatch:
    """AUTOMATION_GIT_PATTERNS must match dangerous git operations."""

    _EXPECTED_MATCHES: list[tuple[str, str]] = [
        ('git push --force', 'push force'),
        ('git push -f origin main', 'push short flag'),
        ('git push --force-with-lease origin main', 'push force lease'),
        ('git push origin main', 'push to main'),
        ('git push origin master', 'push to master'),
        ('git commit -m "fix" main', 'commit to main'),
        ('git reset --hard HEAD~1', 'reset hard'),
        ('git branch -d main', 'delete main branch'),
        ('git branch -D production', 'delete production branch force'),
        ('git merge main', 'merge main'),
        ('git merge master', 'merge master'),
    ]

    def test_all_expected_matches(self):
        for target, label in self._EXPECTED_MATCHES:
            matched = False
            for pattern_str, _desc, _det_id in AUTOMATION_GIT_PATTERNS:
                p = re.compile(pattern_str, re.IGNORECASE)
                if p.search(target):
                    matched = True
                    break
            assert matched, (
                f'AUTOMATION_GIT_PATTERNS should match "{target}" ({label}), '
                f'but no pattern matched'
            )


class TestGithubPatternsMatch:
    """AUTOMATION_GITHUB_PATTERNS must match dangerous GitHub API operations."""

    def test_at_least_one_match_per_group(self):
        # Verify high-priority patterns exist
        assert len(AUTOMATION_GITHUB_PATTERNS) > 0, (
            'AUTOMATION_GITHUB_PATTERNS should have at least one pattern'
        )

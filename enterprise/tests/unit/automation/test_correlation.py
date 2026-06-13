"""Tests for correlation ID utilities."""

import re

from integrations.automation.correlation import (
    build_log_context,
    generate_conversation_title,
    generate_execution_id,
)


class TestGenerateExecutionId:
    def test_generates_valid_format(self):
        """Execution ID matches exec_<12hex> pattern."""
        exec_id = generate_execution_id()
        assert re.match(r'^exec_[a-f0-9]{12}$', exec_id)

    def test_unique_ids(self):
        """Each call generates a unique ID."""
        ids = {generate_execution_id() for _ in range(100)}
        assert len(ids) == 100


class TestGenerateConversationTitle:
    def test_jira_title(self):
        title = generate_conversation_title(
            source_type='jira', jira_issue_key='KAN-17'
        )
        assert '[Automation] Jira KAN-17' == title

    def test_github_title(self):
        title = generate_conversation_title(
            source_type='github', pr_number=42
        )
        assert '[Automation] GitHub PR #42' == title

    def test_default_title(self):
        title = generate_conversation_title(source_type='jira')
        assert '[Automation] jira' == title


class TestBuildLogContext:
    def test_minimal_context(self):
        ctx = build_log_context(execution_id='exec_abc')
        assert ctx['execution_id'] == 'exec_abc'
        assert 'timestamp' in ctx

    def test_full_context(self):
        ctx = build_log_context(
            execution_id='exec_abc',
            conversation_id='conv_123',
            repository='owner/repo',
            branch='feature/test',
            jira_issue_key='KAN-17',
            pr_number=42,
        )
        assert ctx['execution_id'] == 'exec_abc'
        assert ctx['conversation_id'] == 'conv_123'
        assert ctx['repository'] == 'owner/repo'
        assert ctx['branch'] == 'feature/test'
        assert ctx['jira_issue_key'] == 'KAN-17'
        assert ctx['pr_number'] == 42

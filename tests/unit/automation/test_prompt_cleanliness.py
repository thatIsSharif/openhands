"""Tests that rendered prompts contain no git/Jira instructions.

Verifies that prompt templates have been properly cleaned of operational
instructions that should now be handled by the automation platform.
"""

import re

import pytest

from openhands.app_server.automation.prompt_renderer import render_prompt

# Patterns that should NOT appear in any rendered prompt
FORBIDDEN_PATTERNS = [
    r'git clone',
    r'git branch',
    r'git checkout -b',
    r'git add',
    r'git commit',
    r'git push',
    r'comment_endpoint',
    r'token_usage_endpoint',
    r'token_usage_url',
    r'/api/v1/jira/start/comment',
    r'/api/v1/jira/start/token-usage',
    r'/api/v1/git/github/webhook/comment',
    r'POST {{',
    r'"issue_key"',
    r'"token_usage_endpoint"',
]

# Template names and their required context variables
TEMPLATES = {
    'jira_new_conversation.j2': {
        'issue_key': 'KAN-23',
        'title': 'Test issue',
        'issue_type': 'Bug',
        'priority': 'High',
        'description': 'This is a test issue description.',
        'repository': 'owner/repo',
        'repo_label': 'backend',
        'default_branch': 'main',
        'branch': 'bugfix/KAN-23-test-issue',
        'other_repos': [],
    },
    'jira_existing_conversation.j2': {
        'issue_key': 'KAN-23',
        'comment_body': 'Please fix this issue.',
    },
    'github_review_conversation.j2': {
        'pr_number': 42,
        'repository': 'owner/repo',
        'reviewer': 'testuser',
        'review_comment': 'Please fix this.',
        'branch': 'feature/test',
    },
    'github_review_submitted_conversation.j2': {
        'pr_number': 42,
        'repository': 'owner/repo',
        'reviewer': 'testuser',
        'review_state': 'approved',
        'review_comment': 'LGTM!',
        'branch': 'feature/test',
    },
    'github_review_submitted_existing_conversation.j2': {
        'state_label': 'Review',
        'full_name': 'owner/repo',
        'pr_url': 'https://github.com/owner/repo/pull/42',
        'reviewer': 'testuser',
        'review_comment': 'Please fix.',
    },
}


@pytest.mark.parametrize('template_name', list(TEMPLATES.keys()))
def test_no_forbidden_patterns(template_name):
    """Verify rendered prompt contains no git/Jira operational instructions."""
    context = TEMPLATES[template_name]
    rendered = render_prompt(template_name, **context)

    for pattern in FORBIDDEN_PATTERNS:
        if re.search(pattern, rendered, re.IGNORECASE):
            pytest.fail(
                f'Forbidden pattern "{pattern}" found in '
                f'{template_name}:\n{rendered[:500]}'
            )


def test_jira_new_conversation_has_gate_rule():
    """The Jira new conversation prompt should still contain the Gate Rule."""
    rendered = render_prompt(
        'jira_new_conversation.j2',
        issue_key='KAN-23',
        title='Test',
        issue_type='Bug',
        priority='High',
        description='Test',
        repository='owner/repo',
        repo_label='backend',
        default_branch='main',
        branch='bugfix/KAN-23-test',
        other_repos=[],
    )
    assert 'Gate Rule' in rendered
    assert 'Do NOT perform any git operations' in rendered


def test_jira_new_conversation_includes_working_branch():
    """The prompt should include the working branch for context."""
    rendered = render_prompt(
        'jira_new_conversation.j2',
        issue_key='KAN-23',
        title='Test',
        issue_type='Bug',
        priority='High',
        description='Test',
        repository='owner/repo',
        repo_label='backend',
        default_branch='main',
        branch='bugfix/KAN-23-test',
        other_repos=[],
    )
    assert 'bugfix/KAN-23-test' in rendered
    assert 'already created and checked out' in rendered

"""Tests for Jira automation service."""

import hashlib
import hmac
import json

import pytest

from openhands.app_server.automation.jira_automation_service import (
    _validate_repository_format,
    compute_jira_event_id,
    extract_jira_issue_data,
    extract_jira_project_key,
    extract_jira_repository,
    generate_jira_branch_name,
    verify_jira_signature,
)


class TestVerifyJiraSignature:
    def test_valid_signature(self):
        secret = 'test_secret'
        body = json.dumps({'event': 'test'}).encode()
        expected = hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()
        header = f'sha256={expected}'
        assert verify_jira_signature(body, header, secret) is True

    def test_invalid_signature(self):
        secret = 'test_secret'
        body = json.dumps({'event': 'test'}).encode()
        header = 'sha256=invalid_signature'
        assert verify_jira_signature(body, header, secret) is False

    def test_missing_header(self):
        body = json.dumps({'event': 'test'}).encode()
        assert verify_jira_signature(body, None, 'test_secret') is False

    def test_empty_signature(self):
        body = json.dumps({'event': 'test'}).encode()
        assert verify_jira_signature(body, '', 'test_secret') is False

    def test_signature_with_wrong_prefix(self):
        secret = 'test_secret'
        body = json.dumps({'event': 'test'}).encode()
        expected = hmac.new(
            secret.encode(), body, hashlib.sha1
        ).hexdigest()
        header = f'sha1={expected}'
        # SHA1 is not SHA256, so it should fail
        assert verify_jira_signature(body, header, secret) is False


class TestComputeJiraEventId:
    def test_deterministic_for_same_input(self):
        payload = {
            'webhookEvent': 'jira:issue_created',
            'issue': {'id': '12345'},
            'timestamp': 1000000,
        }
        id1 = compute_jira_event_id(payload)
        id2 = compute_jira_event_id(payload)
        assert id1 == id2

    def test_different_for_different_issues(self):
        payload1 = {
            'webhookEvent': 'jira:issue_created',
            'issue': {'id': '12345'},
            'timestamp': 1000000,
        }
        payload2 = {
            'webhookEvent': 'jira:issue_created',
            'issue': {'id': '67890'},
            'timestamp': 1000000,
        }
        assert compute_jira_event_id(payload1) != compute_jira_event_id(payload2)

    def test_returns_string(self):
        payload = {
            'webhookEvent': 'jira:issue_created',
            'issue': {'id': '12345'},
        }
        event_id = compute_jira_event_id(payload)
        assert isinstance(event_id, str)
        assert len(event_id) == 64  # SHA256 hex digest


class TestExtractJiraIssueData:
    def test_extracts_all_fields(self):
        payload = {
            'issue': {
                'key': 'KAN-17',
                'fields': {
                    'summary': 'Test issue',
                    'description': 'Test description',
                    'issuetype': {'name': 'Story'},
                    'priority': {'name': 'Medium'},
                    'reporter': {'displayName': 'User1'},
                    'labels': ['automation'],
                    'project': {'key': 'KAN'},
                },
            }
        }
        data = extract_jira_issue_data(payload)
        assert data is not None
        assert data['issue_key'] == 'KAN-17'
        assert data['summary'] == 'Test issue'
        assert data['description'] == 'Test description'
        assert data['issue_type'] == 'Story'
        assert data['priority'] == 'Medium'
        assert data['reporter'] == 'User1'
        assert data['labels'] == ['automation']
        assert data['project_key'] == 'KAN'

    def test_missing_issue_key(self):
        payload = {'issue': {'fields': {}}}
        assert extract_jira_issue_data(payload) is None

    def test_missing_optional_fields(self):
        payload = {
            'issue': {
                'key': 'KAN-17',
                'fields': {
                    'summary': 'Test',
                },
            }
        }
        data = extract_jira_issue_data(payload)
        assert data is not None
        assert data['description'] == ''
        assert data['issue_type'] == ''
        assert data['priority'] == ''
        assert data['labels'] == []

    def test_no_reporter(self):
        payload = {
            'issue': {
                'key': 'KAN-17',
                'fields': {
                    'summary': 'Test',
                    'reporter': None,
                },
            }
        }
        data = extract_jira_issue_data(payload)
        assert data is not None
        assert data['reporter'] == ''


class TestGenerateJiraBranchName:
    def test_feature_branch(self):
        branch = generate_jira_branch_name(
            'KAN-17', 'Story', 'Implement automation platform'
        )
        assert branch == 'feature/KAN-17-implement-automation-platform'

    def test_bugfix_branch(self):
        branch = generate_jira_branch_name(
            'KAN-17', 'Bug', 'Fix null pointer exception'
        )
        assert branch == 'bugfix/KAN-17-fix-null-pointer-exception'

    def test_slug_truncation(self):
        long_summary = 'a' * 100
        branch = generate_jira_branch_name('KAN-17', 'Story', long_summary)
        assert len(branch) < 100

    def test_special_characters_in_summary(self):
        branch = generate_jira_branch_name(
            'KAN-17', 'Story', 'Implement @#$%^& feature!'
        )
        assert 'feature' in branch
        assert all(
            c.isalnum() or c in '-/' for c in branch
        ), f'Branch has invalid chars: {branch}'


class TestExtractJiraProjectKey:
    def test_extracts_from_full_payload(self):
        payload = {
            'issue': {
                'fields': {
                    'project': {'key': 'KAN'},
                },
            },
        }
        assert extract_jira_project_key(payload) == 'KAN'

    def test_returns_none_when_missing(self):
        assert extract_jira_project_key({}) is None
        assert extract_jira_project_key({'issue': {}}) is None
        assert extract_jira_project_key({'issue': {'fields': {}}}) is None


class TestExtractJiraRepository:
    def test_extracts_from_customfield(self):
        """Extracts repository from the configured custom field."""
        payload = {
            'issue': {
                'fields': {
                    'summary': 'Test issue',
                    'customfield_10010': 'thatIsSharif/workflow-engine',
                },
            },
        }
        result = extract_jira_repository(payload)
        assert result == 'thatIsSharif/workflow-engine'

    def test_extracts_dict_value(self):
        """Extracts repository from a dict-style custom field value."""
        payload = {
            'issue': {
                'fields': {
                    'customfield_10010': {'value': 'thatIsSharif/dsd-frontend'},
                },
            },
        }
        result = extract_jira_repository(payload)
        assert result == 'thatIsSharif/dsd-frontend'

    def test_extracts_dict_with_name(self):
        """Extracts repository from a dict with 'name' key."""
        payload = {
            'issue': {
                'fields': {
                    'customfield_10010': {'name': 'thatIsSharif/devops'},
                },
            },
        }
        result = extract_jira_repository(payload)
        assert result == 'thatIsSharif/devops'

    def test_returns_none_when_missing(self):
        """Returns None when no repository field is present."""
        payload = {
            'issue': {
                'fields': {
                    'summary': 'Test issue',
                },
            },
        }
        assert extract_jira_repository(payload) is None

    def test_returns_none_when_null(self):
        """Returns None when the repository field is null."""
        payload = {
            'issue': {
                'fields': {
                    'customfield_10010': None,
                },
            },
        }
        assert extract_jira_repository(payload) is None

    def test_returns_none_for_empty_payload(self):
        """Returns None for empty payload."""
        assert extract_jira_repository({}) is None


class TestValidateRepositoryFormat:
    def test_valid_format(self):
        assert _validate_repository_format('owner/repo') is True

    def test_valid_format_with_multi_part(self):
        assert _validate_repository_format('thatIsSharif/workflow-engine') is True

    def test_invalid_no_slash(self):
        assert _validate_repository_format('justrepo') is False

    def test_invalid_empty_owner(self):
        assert _validate_repository_format('/repo') is False

    def test_invalid_empty_repo(self):
        assert _validate_repository_format('owner/') is False

    def test_invalid_empty_string(self):
        assert _validate_repository_format('') is False


class TestComputeJiraEventIdWithRepo:
    """Tests for compute_jira_event_id with repo suffix."""

    def test_different_repos_get_different_ids(self):
        payload = {
            'webhookEvent': 'jira:issue_created',
            'issue': {'id': '12345'},
            'timestamp': 1000000,
        }
        id1 = compute_jira_event_id(payload, repo='owner/repo-a')
        id2 = compute_jira_event_id(payload, repo='owner/repo-b')
        assert id1 != id2, 'Different repos must produce different event IDs'

    def test_repo_suffix_is_deterministic(self):
        payload = {
            'webhookEvent': 'jira:issue_created',
            'issue': {'id': '12345'},
            'timestamp': 1000000,
        }
        id1 = compute_jira_event_id(payload, repo='owner/repo')
        id2 = compute_jira_event_id(payload, repo='owner/repo')
        assert id1 == id2

    def test_without_repo_matches_legacy_behavior(self):
        payload = {
            'webhookEvent': 'jira:issue_created',
            'issue': {'id': '12345'},
            'timestamp': 1000000,
        }
        legacy_id = compute_jira_event_id(payload)
        no_repo_id = compute_jira_event_id(payload, repo=None)
        assert legacy_id == no_repo_id

    def test_repo_id_differs_from_no_repo_id(self):
        payload = {
            'webhookEvent': 'jira:issue_created',
            'issue': {'id': '12345'},
            'timestamp': 1000000,
        }
        without = compute_jira_event_id(payload)
        with_repo = compute_jira_event_id(payload, repo='owner/repo')
        assert without != with_repo, 'Repo suffix must change the event ID'




class TestProcessIssueCreated:
    """Tests for the single-execution flow in JiraAutomationService."""

    @pytest.mark.asyncio
    async def test_multi_repo_backend_primary(self):
        """With backend+frontend repos, backend is primary (default)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from openhands.app_server.automation.execution_models import (
            JiraProjectRepositoryRecord,
        )
        from openhands.app_server.automation.jira_automation_service import (
            JiraAutomationService,
        )

        payload = {
            'webhookEvent': 'jira:issue_created',
            'issue_event_type_name': 'issue_assigned',
            'issue': {
                'id': '10001',
                'key': 'KAN-42',
                'fields': {
                    'summary': 'Multi-repo task',
                    'description': 'Needs backend and frontend changes',
                    'issuetype': {'name': 'Story'},
                    'priority': {'name': 'Medium'},
                    'reporter': {'displayName': 'Dev'},
                    'labels': [],
                    'project': {'key': 'KAN'},
                },
            },
            'changelog': {
                'items': [{'field': 'assignee', 'to': 'target-user'}]
            },
            'timestamp': 2000000,
        }

        repo_backend = JiraProjectRepositoryRecord(
            id=1, jira_project_key='KAN',
            repository='workflow-engine', owner='thatIsSharif',
            default_branch='main', label='backend',
        )
        repo_frontend = JiraProjectRepositoryRecord(
            id=2, jira_project_key='KAN',
            repository='dsd-frontend', owner='thatIsSharif',
            default_branch='main', label='frontend',
        )

        with patch(
            'openhands.app_server.automation.jira_automation_service.ComplexityAnalyzer.from_env'
        ) as mock_from_env:
            mock_analyzer = MagicMock()
            mock_analyzer.analyze = AsyncMock(
                return_value=MagicMock(
                    complexity='medium', task_type='backend', reasoning='test'
                )
            )
            mock_from_env.return_value = mock_analyzer

            mock_store = AsyncMock()
            mock_store.get_jira_project_repos_by_project_key = AsyncMock(
                return_value=[repo_backend, repo_frontend]
            )

            mock_execution_service = MagicMock()
            mock_execution_service.store = mock_store
            mock_execution_service.create_execution = AsyncMock(
                return_value=(MagicMock(execution_id='exec-1'), True)
            )
            mock_execution_service.transition_state = AsyncMock()

            mock_openhands_client = MagicMock()
            mock_openhands_client.create_conversation = AsyncMock(
                return_value='conv-1'
            )

            service = JiraAutomationService(
                execution_service=mock_execution_service,
                openhands_client=mock_openhands_client,
            )

            mock_request = MagicMock()
            mock_request.base_url = 'http://localhost:8000'
            mock_state = MagicMock()

            result = await service.process_issue_created(
                payload=payload,
                state=mock_state,
                request=mock_request,
            )

        # Single execution with backend as primary
        assert result['status'] == 'running'
        assert result['execution_id'] == 'exec-1'
        assert result['repository'] == 'thatIsSharif/workflow-engine'
        assert 'thatIsSharif/dsd-frontend' in result['other_repos']

        # Verify repos were resolved from DB
        mock_store.get_jira_project_repos_by_project_key.assert_called_once_with('KAN')

        # One execution created with backend repo
        assert mock_execution_service.create_execution.call_count == 1
        assert mock_execution_service.create_execution.call_args[1]['repository'] == (
            'thatIsSharif/workflow-engine'
        )

        # One conversation created
        assert mock_openhands_client.create_conversation.call_count == 1
        assert mock_openhands_client.create_conversation.call_args[1]['repository'] == (
            'thatIsSharif/workflow-engine'
        )

    @pytest.mark.asyncio
    async def test_multi_repo_frontend_task_type(self):
        """When task_type='frontend' and a frontend repo exists, it becomes primary."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from openhands.app_server.automation.execution_models import (
            JiraProjectRepositoryRecord,
        )
        from openhands.app_server.automation.jira_automation_service import (
            JiraAutomationService,
        )

        payload = {
            'webhookEvent': 'jira:issue_created',
            'issue_event_type_name': 'issue_assigned',
            'issue': {
                'id': '10002',
                'key': 'KAN-43',
                'fields': {
                    'summary': 'Frontend-only task',
                    'description': 'Only UI changes needed',
                    'issuetype': {'name': 'Task'},
                    'priority': {'name': 'High'},
                    'reporter': {'displayName': 'Dev'},
                    'labels': [],
                    'project': {'key': 'KAN'},
                },
            },
            'changelog': {
                'items': [{'field': 'assignee', 'to': 'target-user'}]
            },
            'timestamp': 3000000,
        }

        repo_backend = JiraProjectRepositoryRecord(
            id=1, jira_project_key='KAN',
            repository='workflow-engine', owner='thatIsSharif',
            default_branch='main', label='backend',
        )
        repo_frontend = JiraProjectRepositoryRecord(
            id=2, jira_project_key='KAN',
            repository='dsd-frontend', owner='thatIsSharif',
            default_branch='main', label='frontend',
        )

        with patch(
            'openhands.app_server.automation.jira_automation_service.ComplexityAnalyzer.from_env'
        ) as mock_from_env:
            mock_analyzer = MagicMock()
            mock_analyzer.analyze = AsyncMock(
                return_value=MagicMock(
                    complexity='low', task_type='frontend', reasoning='simple UI change'
                )
            )
            mock_from_env.return_value = mock_analyzer

            mock_store = AsyncMock()
            mock_store.get_jira_project_repos_by_project_key = AsyncMock(
                return_value=[repo_backend, repo_frontend]
            )

            mock_execution_service = MagicMock()
            mock_execution_service.store = mock_store
            mock_execution_service.create_execution = AsyncMock(
                return_value=(MagicMock(execution_id='exec-2'), True)
            )
            mock_execution_service.transition_state = AsyncMock()

            mock_openhands_client = MagicMock()
            mock_openhands_client.create_conversation = AsyncMock(
                return_value='conv-2'
            )

            service = JiraAutomationService(
                execution_service=mock_execution_service,
                openhands_client=mock_openhands_client,
            )

            mock_request = MagicMock()
            mock_request.base_url = 'http://localhost:8000'
            mock_state = MagicMock()

            result = await service.process_issue_created(
                payload=payload,
                state=mock_state,
                request=mock_request,
            )

        # Frontend repo is primary
        assert result['status'] == 'running'
        assert result['repository'] == 'thatIsSharif/dsd-frontend'
        assert 'thatIsSharif/workflow-engine' in result['other_repos']

        assert mock_execution_service.create_execution.call_args[1]['repository'] == (
            'thatIsSharif/dsd-frontend'
        )
        assert mock_openhands_client.create_conversation.call_args[1]['repository'] == (
            'thatIsSharif/dsd-frontend'
        )

    @pytest.mark.asyncio
    async def test_hybrid_task_uses_backend_primary(self):
        """When task_type='hybrid', backend is primary (frontend cloned by agent)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from openhands.app_server.automation.execution_models import (
            JiraProjectRepositoryRecord,
        )
        from openhands.app_server.automation.jira_automation_service import (
            JiraAutomationService,
        )

        payload = {
            'webhookEvent': 'jira:issue_created',
            'issue_event_type_name': 'issue_assigned',
            'issue': {
                'id': '10003',
                'key': 'KAN-44',
                'fields': {
                    'summary': 'Hybrid task',
                    'description': 'API + UI changes needed',
                    'issuetype': {'name': 'Story'},
                    'priority': {'name': 'High'},
                    'reporter': {'displayName': 'Dev'},
                    'labels': [],
                    'project': {'key': 'KAN'},
                },
            },
            'changelog': {
                'items': [{'field': 'assignee', 'to': 'target-user'}]
            },
            'timestamp': 4000000,
        }

        repo_backend = JiraProjectRepositoryRecord(
            id=1, jira_project_key='KAN',
            repository='workflow-engine', owner='thatIsSharif',
            default_branch='main', label='backend',
        )
        repo_frontend = JiraProjectRepositoryRecord(
            id=2, jira_project_key='KAN',
            repository='dsd-frontend', owner='thatIsSharif',
            default_branch='main', label='frontend',
        )

        with patch(
            'openhands.app_server.automation.jira_automation_service.ComplexityAnalyzer.from_env'
        ) as mock_from_env:
            mock_analyzer = MagicMock()
            mock_analyzer.analyze = AsyncMock(
                return_value=MagicMock(
                    complexity='complex', task_type='hybrid', reasoning='both layers'
                )
            )
            mock_from_env.return_value = mock_analyzer

            mock_store = AsyncMock()
            mock_store.get_jira_project_repos_by_project_key = AsyncMock(
                return_value=[repo_backend, repo_frontend]
            )

            mock_execution_service = MagicMock()
            mock_execution_service.store = mock_store
            mock_execution_service.create_execution = AsyncMock(
                return_value=(MagicMock(execution_id='exec-3'), True)
            )
            mock_execution_service.transition_state = AsyncMock()

            mock_openhands_client = MagicMock()
            mock_openhands_client.create_conversation = AsyncMock(
                return_value='conv-3'
            )

            service = JiraAutomationService(
                execution_service=mock_execution_service,
                openhands_client=mock_openhands_client,
            )

            mock_request = MagicMock()
            mock_request.base_url = 'http://localhost:8000'
            mock_state = MagicMock()

            result = await service.process_issue_created(
                payload=payload,
                state=mock_state,
                request=mock_request,
            )

        # Hybrid → backend primary
        assert result['status'] == 'running'
        assert result['repository'] == 'thatIsSharif/workflow-engine'
        assert 'thatIsSharif/dsd-frontend' in result['other_repos']

    @pytest.mark.asyncio
    async def test_analysis_failure_falls_back_to_backend(self):
        """When complexity analysis returns None, fall back to backend primary."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from openhands.app_server.automation.execution_models import (
            JiraProjectRepositoryRecord,
        )
        from openhands.app_server.automation.jira_automation_service import (
            JiraAutomationService,
        )

        payload = {
            'webhookEvent': 'jira:issue_created',
            'issue_event_type_name': 'issue_assigned',
            'issue': {
                'id': '10004',
                'key': 'KAN-45',
                'fields': {
                    'summary': 'Analysis fails task',
                    'description': 'Should fallback to default',
                    'issuetype': {'name': 'Bug'},
                    'priority': {'name': 'Low'},
                    'reporter': {'displayName': 'Dev'},
                    'labels': [],
                    'project': {'key': 'KAN'},
                },
            },
            'changelog': {
                'items': [{'field': 'assignee', 'to': 'target-user'}]
            },
            'timestamp': 5000000,
        }

        repo_backend = JiraProjectRepositoryRecord(
            id=1, jira_project_key='KAN',
            repository='workflow-engine', owner='thatIsSharif',
            default_branch='main', label='backend',
        )
        repo_frontend = JiraProjectRepositoryRecord(
            id=2, jira_project_key='KAN',
            repository='dsd-frontend', owner='thatIsSharif',
            default_branch='main', label='frontend',
        )

        with patch(
            'openhands.app_server.automation.jira_automation_service.ComplexityAnalyzer.from_env'
        ) as mock_from_env:
            mock_analyzer = MagicMock()
            mock_analyzer.analyze = AsyncMock(return_value=None)
            mock_from_env.return_value = mock_analyzer

            mock_store = AsyncMock()
            mock_store.get_jira_project_repos_by_project_key = AsyncMock(
                return_value=[repo_backend, repo_frontend]
            )

            mock_execution_service = MagicMock()
            mock_execution_service.store = mock_store
            mock_execution_service.create_execution = AsyncMock(
                return_value=(MagicMock(execution_id='exec-4'), True)
            )
            mock_execution_service.transition_state = AsyncMock()

            mock_openhands_client = MagicMock()
            mock_openhands_client.create_conversation = AsyncMock(
                return_value='conv-4'
            )

            service = JiraAutomationService(
                execution_service=mock_execution_service,
                openhands_client=mock_openhands_client,
            )

            mock_request = MagicMock()
            mock_request.base_url = 'http://localhost:8000'
            mock_state = MagicMock()

            result = await service.process_issue_created(
                payload=payload,
                state=mock_state,
                request=mock_request,
            )

        # Fallback → backend primary (existing behaviour)
        assert result['status'] == 'running'
        assert result['repository'] == 'thatIsSharif/workflow-engine'

    @pytest.mark.asyncio
    async def test_single_repo_still_works(self):
        """When the DB has only 1 repo, result should be backward-compatible."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from openhands.app_server.automation.execution_models import (
            JiraProjectRepositoryRecord,
        )
        from openhands.app_server.automation.jira_automation_service import (
            JiraAutomationService,
        )

        payload = {
            'webhookEvent': 'jira:issue_created',
            'issue_event_type_name': 'issue_assigned',
            'issue': {
                'id': '10002',
                'key': 'KAN-43',
                'fields': {
                    'summary': 'Single repo task',
                    'description': 'Only backend change',
                    'issuetype': {'name': 'Task'},
                    'priority': {'name': 'High'},
                    'reporter': {'displayName': 'Dev'},
                    'labels': [],
                    'project': {'key': 'KAN'},
                },
            },
            'changelog': {
                'items': [{'field': 'assignee', 'to': 'target-user'}]
            },
            'timestamp': 3000000,
        }

        repo_single = JiraProjectRepositoryRecord(
            id=3, jira_project_key='KAN',
            repository='workflow-engine', owner='thatIsSharif',
            default_branch='main', label='backend',
        )

        with patch(
            'openhands.app_server.automation.jira_automation_service.ComplexityAnalyzer.from_env'
        ) as mock_from_env:
            mock_analyzer = MagicMock()
            mock_analyzer.analyze = AsyncMock(
                return_value=MagicMock(
                    complexity='medium', task_type='backend', reasoning='test'
                )
            )
            mock_from_env.return_value = mock_analyzer

            mock_store = AsyncMock()
            mock_store.get_jira_project_repos_by_project_key = AsyncMock(
                return_value=[repo_single]
            )

            mock_execution_service = MagicMock()
            mock_execution_service.store = mock_store
            mock_execution_service.create_execution = AsyncMock(
                return_value=(MagicMock(execution_id='exec-3'), True)
            )
            mock_execution_service.transition_state = AsyncMock()

            mock_openhands_client = MagicMock()
            mock_openhands_client.create_conversation = AsyncMock(
                return_value='conv-3'
            )

            service = JiraAutomationService(
                execution_service=mock_execution_service,
                openhands_client=mock_openhands_client,
            )

            mock_request = MagicMock()
            mock_request.base_url = 'http://localhost:8000'
            mock_state = MagicMock()

            result = await service.process_issue_created(
                payload=payload,
                state=mock_state,
                request=mock_request,
            )

        # Single repo -> backward-compatible single result dict
        assert result['status'] == 'running'
        assert result['execution_id'] == 'exec-3'
        assert result['repository'] == 'thatIsSharif/workflow-engine'

    @pytest.mark.asyncio
    async def test_no_repos_configured_returns_error(self):
        """When no repos are configured for the project, return an error."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from openhands.app_server.automation.jira_automation_service import (
            JiraAutomationService,
        )

        payload = {
            'webhookEvent': 'jira:issue_created',
            'issue_event_type_name': 'issue_assigned',
            'issue': {
                'id': '10003',
                'key': 'UNKNOWN-1',
                'fields': {
                    'summary': 'No repo task',
                    'description': 'No repos configured',
                    'issuetype': {'name': 'Task'},
                    'priority': {'name': 'Low'},
                    'reporter': {'displayName': 'Dev'},
                    'labels': [],
                    'project': {'key': 'UNKNOWN'},
                },
            },
            'changelog': {
                'items': [{'field': 'assignee', 'to': 'target-user'}]
            },
            'timestamp': 4000000,
        }

        with patch(
            'openhands.app_server.automation.jira_automation_service.ComplexityAnalyzer.from_env'
        ) as mock_from_env:
            mock_analyzer = MagicMock()
            mock_analyzer.analyze = AsyncMock(
                return_value=MagicMock(
                    complexity='low', task_type='backend', reasoning='test'
                )
            )
            mock_from_env.return_value = mock_analyzer

            mock_store = AsyncMock()
            mock_store.get_jira_project_repos_by_project_key = AsyncMock(
                return_value=[]
            )

            mock_execution_service = MagicMock()
            mock_execution_service.store = mock_store

            mock_openhands_client = MagicMock()

            service = JiraAutomationService(
                execution_service=mock_execution_service,
                openhands_client=mock_openhands_client,
            )

            mock_request = MagicMock()
            mock_request.base_url = 'http://localhost:8000'
            mock_state = MagicMock()

            result = await service.process_issue_created(
                payload=payload,
                state=mock_state,
                request=mock_request,
            )

        assert result['status'] == 'failed'
        assert 'No repositories configured' in result['error']
        assert mock_openhands_client.create_conversation.call_count == 0

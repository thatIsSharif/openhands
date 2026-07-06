"""Tests for ComplexityAnalyzer."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openhands.app_server.automation.complexity_analyzer import (
    ComplexityAnalyzer,
    ComplexityResult,
)

_ISSUE_DATA = {
    'issue_key': 'PROJ-123',
    'issue_type': 'Task',
    'priority': 'Medium',
    'summary': 'Add rate limiting to the API',
    'description': 'We need rate limiting on all public endpoints.',
}


def _make_response(content: str) -> MagicMock:
    """Build a mock litellm response object."""
    choice = MagicMock()
    choice.message.content = content
    response = MagicMock()
    response.choices = [choice]
    return response


class TestComplexityAnalyzerParseResponse:
    # ── JSON responses (backward compatible) ──────────────────────

    def test_parses_complex_json(self):
        data = {'complexity': 'complex', 'reasoning': 'spans many services'}
        result = ComplexityAnalyzer._parse_response(json.dumps(data))
        assert result == ComplexityResult(
            complexity='complex', reasoning='spans many services'
        )

    def test_parses_json_with_markdown_fences(self):
        data = {'complexity': 'medium', 'reasoning': '2 files'}
        raw = f'```json\n{json.dumps(data)}\n```'
        result = ComplexityAnalyzer._parse_response(raw)
        assert result == ComplexityResult(complexity='medium', reasoning='2 files')

    def test_returns_none_for_json_unknown_tier(self):
        data = {'complexity': 'super-hard', 'reasoning': '...'}
        result = ComplexityAnalyzer._parse_response(json.dumps(data))
        assert result is None

    # ── Plain-text fallback ──────────────────────────────────────

    def test_extracts_complex_from_text(self):
        result = ComplexityAnalyzer._parse_response(
            'This task requires complex changes across the system.'
        )
        assert result is not None
        assert result.complexity == 'complex'

    def test_extracts_medium_from_text(self):
        result = ComplexityAnalyzer._parse_response('medium')
        assert result is not None
        assert result.complexity == 'medium'

    def test_extracts_low_from_text(self):
        result = ComplexityAnalyzer._parse_response('The complexity is low')
        assert result is not None
        assert result.complexity == 'low'

    def test_low_word_boundary_excludes_below(self):
        """``below`` must NOT match ``low``."""
        result = ComplexityAnalyzer._parse_response('see below for details')
        assert result is None

    def test_complex_matches_first(self):
        """When multiple tiers appear, the first in order wins."""
        result = ComplexityAnalyzer._parse_response('low medium complex')
        assert result is not None
        assert result.complexity == 'complex'

    def test_returns_none_for_unrelated_text(self):
        result = ComplexityAnalyzer._parse_response('not json at all')
        assert result is None

    def test_returns_none_for_empty_content(self):
        result = ComplexityAnalyzer._parse_response('')
        assert result is None


@pytest.mark.asyncio
class TestComplexityAnalyzerAnalyze:
    async def test_analyze_returns_result_on_success(self):
        data = {'complexity': 'low', 'reasoning': 'simple typo'}
        with patch(
            'openhands.app_server.automation.complexity_analyzer.litellm.acompletion',
            new_callable=AsyncMock,
            return_value=_make_response(json.dumps(data)),
        ):
            analyzer = ComplexityAnalyzer(
                api_key='test-key',
                base_url='https://opencode.ai/zen/go/v1',
            )
            result = await analyzer.analyze(_ISSUE_DATA)
            assert result == ComplexityResult(complexity='low', reasoning='simple typo')

    async def test_analyze_plain_text_response(self):
        """LLM returns a plain word — fallback parsing extracts it."""
        with patch(
            'openhands.app_server.automation.complexity_analyzer.litellm.acompletion',
            new_callable=AsyncMock,
            return_value=_make_response('medium'),
        ):
            analyzer = ComplexityAnalyzer(
                api_key='test-key',
                base_url='https://opencode.ai/zen/go/v1',
            )
            result = await analyzer.analyze(_ISSUE_DATA)
            assert result is not None
            assert result.complexity == 'medium'

    async def test_analyze_returns_none_on_llm_failure(self):
        with patch(
            'openhands.app_server.automation.complexity_analyzer.litellm.acompletion',
            new_callable=AsyncMock,
            side_effect=RuntimeError('API down'),
        ):
            analyzer = ComplexityAnalyzer(
                api_key='test-key',
                base_url='https://opencode.ai/zen/go/v1',
            )
            result = await analyzer.analyze(_ISSUE_DATA)
            assert result is None

    async def test_analyze_returns_none_on_empty_content(self):
        with patch(
            'openhands.app_server.automation.complexity_analyzer.litellm.acompletion',
            new_callable=AsyncMock,
            return_value=_make_response(None),
        ):
            analyzer = ComplexityAnalyzer(
                api_key='test-key',
                base_url='https://opencode.ai/zen/go/v1',
            )
            result = await analyzer.analyze(_ISSUE_DATA)
            assert result is None

    async def test_analyze_passes_api_key_and_base_url(self):
        data = {'complexity': 'medium', 'reasoning': 'moderate changes'}
        mock = AsyncMock(return_value=_make_response(json.dumps(data)))
        with patch(
            'openhands.app_server.automation.complexity_analyzer.litellm.acompletion',
            new=mock,
        ):
            analyzer = ComplexityAnalyzer(
                api_key='sk-abc',
                base_url='https://gateway.example.com/v1',
            )
            await analyzer.analyze(_ISSUE_DATA)

            call_kwargs = mock.call_args.kwargs
            assert call_kwargs['api_key'] == 'sk-abc'
            assert call_kwargs['api_base'] == 'https://gateway.example.com/v1'
            assert call_kwargs['model'] == 'openai/deepseek-v4-flash-free'
            assert call_kwargs['max_tokens'] == 50
            assert call_kwargs['timeout'] == 15

"""Tests for ComplexityRouter."""

import os
from unittest.mock import patch

from openhands.app_server.automation.complexity_router import ComplexityRouter


class TestComplexityRouterFromEnv:
    def test_all_env_vars_set(self):
        with patch.dict(
            os.environ,
            {
                'OH_JIRA_COMPLEX_MODEL': 'openai/gpt-5',
                'OH_JIRA_MEDIUM_MODEL': 'openai/gpt-4.1',
                'OH_JIRA_LOW_MODEL': 'openai/gpt-4.1-mini',
            },
            clear=True,
        ):
            router = ComplexityRouter.from_env()
            assert router.complex_model == 'openai/gpt-5'
            assert router.medium_model == 'openai/gpt-4.1'
            assert router.low_model == 'openai/gpt-4.1-mini'

    def test_no_env_vars_set(self):
        with patch.dict(os.environ, {}, clear=True):
            router = ComplexityRouter.from_env()
            assert router.complex_model is None
            assert router.medium_model is None
            assert router.low_model is None

    def test_partial_env_vars(self):
        with patch.dict(
            os.environ,
            {
                'OH_JIRA_COMPLEX_MODEL': 'openai/gpt-5',
            },
            clear=True,
        ):
            router = ComplexityRouter.from_env()
            assert router.complex_model == 'openai/gpt-5'
            assert router.medium_model is None
            assert router.low_model is None


class TestComplexityRouterIsEnabled:
    def test_enabled_when_all_set(self):
        router = ComplexityRouter(
            complex_model='openai/gpt-5',
            medium_model='openai/gpt-4.1',
            low_model='openai/gpt-4.1-mini',
        )
        assert router.is_enabled is True

    def test_disabled_when_one_missing(self):
        router = ComplexityRouter(
            complex_model='openai/gpt-5',
            medium_model=None,
            low_model='openai/gpt-4.1-mini',
        )
        assert router.is_enabled is False

    def test_disabled_when_all_none(self):
        router = ComplexityRouter(
            complex_model=None,
            medium_model=None,
            low_model=None,
        )
        assert router.is_enabled is False


class TestComplexityRouterResolve:
    def test_resolve_complex(self):
        router = ComplexityRouter(
            complex_model='openai/deepseek-v4-pro',
            medium_model='openai/MiMo-V2.5',
            low_model='openai/MiniMax M3',
        )
        assert router.resolve('complex') == 'openai/deepseek-v4-pro'

    def test_resolve_medium(self):
        router = ComplexityRouter(
            complex_model='openai/deepseek-v4-pro',
            medium_model='openai/MiMo-V2.5',
            low_model='openai/MiniMax M3',
        )
        assert router.resolve('medium') == 'openai/MiMo-V2.5'

    def test_resolve_low(self):
        router = ComplexityRouter(
            complex_model='openai/deepseek-v4-pro',
            medium_model='openai/MiMo-V2.5',
            low_model='openai/MiniMax M3',
        )
        assert router.resolve('low') == 'openai/MiniMax M3'

    def test_resolve_unknown_tier(self):
        router = ComplexityRouter(
            complex_model='openai/deepseek-v4-pro',
            medium_model='openai/MiMo-V2.5',
            low_model='openai/MiniMax M3',
        )
        assert router.resolve('bogus') is None

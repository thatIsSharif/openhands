"""Complexity analyzer — lightweight LLM call to classify Jira task complexity.

Makes a single stateless LiteLLM completion (no agent, no sandbox, no
conversation) and returns a structured classification.  On any failure the
caller falls back to the user's default model — complexity routing is a
best-effort optimisation, never a hard gate.
"""

from __future__ import annotations

import json
import os
import re
import warnings
from dataclasses import dataclass
from typing import Literal

with warnings.catch_warnings():
    warnings.simplefilter('ignore')
    import litellm

from openhands.app_server.utils.logger import openhands_logger as logger

ComplexityTier = Literal['complex', 'medium', 'low']

ANALYSIS_PROMPT = """\
Classify this Jira issue's complexity for a software engineering AI agent.

COMPLEX — Architectural decisions, 3+ files/modules, schema changes, API
  design, auth, cross-service coordination, high ambiguity.
MEDIUM — 2-3 files, moderate logic, business logic, tests, config changes.
  Requirements are reasonably clear.
LOW — Single-file change, simple bug fix, typo/formatting, minor config,
  documentation, dependency bump. Requirements are unambiguous.

Jira Issue:
  Key: {issue_key}
  Type: {issue_type}
  Priority: {priority}
  Summary: {summary}
  Description: {description}

Return ONLY ONE WORD: complex, medium, or low."""


@dataclass(frozen=True)
class ComplexityResult:
    complexity: ComplexityTier
    reasoning: str


@dataclass
class ComplexityAnalyzer:
    """Classifies a Jira issue's complexity via a single LiteLLM completion."""

    model: str = 'openai/deepseek-v4-flash-free'
    api_key: str | None = None
    base_url: str = 'https://opencode.ai/zen/go/v1'
    timeout: int = 15

    @classmethod
    def from_env(cls) -> 'ComplexityAnalyzer':
        return cls(
            model=os.getenv('OH_JIRA_ANALYSIS_MODEL', cls.model),
            api_key=os.getenv('OH_JIRA_ANALYSIS_API_KEY'),
            base_url=os.getenv('OH_JIRA_ANALYSIS_BASE_URL', cls.base_url),
        )

    async def analyze(self, issue_data: dict) -> ComplexityResult | None:
        """Classify *issue_data* and return a :class:`ComplexityResult`.

        Returns ``None`` on any failure so callers can safely fall back to
        the default model.
        """
        prompt = ANALYSIS_PROMPT.format(
            issue_key=issue_data.get('issue_key', ''),
            issue_type=issue_data.get('issue_type', ''),
            priority=issue_data.get('priority', ''),
            summary=issue_data.get('summary', ''),
            description=issue_data.get('description', ''),
        )

        kwargs: dict = {
            'model': self.model,
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': 50,
            'timeout': self.timeout,
        }
        if self.api_key:
            kwargs['api_key'] = self.api_key
        if self.base_url:
            kwargs['api_base'] = self.base_url

        try:
            response = await litellm.acompletion(**kwargs)
            content = response.choices[0].message.content
            if content is None:
                logger.warning('[ComplexityAnalyzer] LLM returned empty content')
                return None

            return self._parse_response(content)

        except Exception:
            logger.warning(
                '[ComplexityAnalyzer] LLM call failed',
                exc_info=True,
            )
            return None

    @staticmethod
    def _parse_response(content: str) -> ComplexityResult | None:
        """Extract a complexity tier from *content*.

        Tries JSON first (backward compatible), then falls back to finding
        the first occurrence of ``complex``, ``medium``, or ``low`` in the text.
        """
        cleaned = content.strip().lower()

        # Try JSON first (supports old prompt format)
        try:
            # Strip markdown code fences if present
            stripped = re.sub(r'^```(?:json)?\s*', '', cleaned)
            stripped = re.sub(r'\s*```$', '', stripped)
            data = json.loads(stripped)
            complexity = data.get('complexity', '').lower().strip()
            if complexity in ('complex', 'medium', 'low'):
                return ComplexityResult(
                    complexity=complexity,  # type: ignore[arg-type]
                    reasoning=data.get('reasoning', ''),
                )
        except (json.JSONDecodeError, TypeError):
            pass

        # Fallback: scan for the first complexity word in the text.
        # Use word-boundary match so "low" doesn't match "below"/"follow"/"allow".
        for tier in ('complex', 'medium', 'low'):
            if re.search(rf'\b{re.escape(tier)}\b', cleaned):
                return ComplexityResult(complexity=tier, reasoning=content[:200])

        logger.warning(
            '[ComplexityAnalyzer] No complexity tier found in response: %s',
            content[:200],
        )
        return None

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
You are a task complexity classifier for a software engineering AI agent.
Analyze the following Jira issue and classify its complexity.

Classification guidelines:

COMPLEX — Requires architectural decisions, spans 3+ files/modules,
  involves database schema changes, API design, authentication/authorization,
  or cross-service coordination. High ambiguity in requirements.

MEDIUM — Requires changes across 2-3 files, moderate logic changes,
  involves business logic updates, test additions, or configuration changes.
  Requirements are reasonably clear.

LOW — Single-file change, simple bug fix, typo/formatting, minor config
  update, documentation, or dependency bump. Requirements are unambiguous.

Jira Issue:
  Key: {issue_key}
  Type: {issue_type}
  Priority: {priority}
  Summary: {summary}
  Description: {description}

Respond with a JSON object:
  {{"complexity": "complex|medium|low", "reasoning": "brief explanation"}}"""


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
            'max_tokens': 500,
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
        """Extract a JSON object from *content* and build a result."""
        # Strip markdown code fences if present
        cleaned = re.sub(r'^```(?:json)?\s*', '', content.strip())
        cleaned = re.sub(r'\s*```$', '', cleaned)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning(
                '[ComplexityAnalyzer] Failed to parse LLM response as JSON: %s',
                content[:200],
            )
            return None

        complexity = data.get('complexity', '').lower().strip()
        if complexity not in ('complex', 'medium', 'low'):
            logger.warning(
                '[ComplexityAnalyzer] Unknown complexity tier: %r',
                complexity,
            )
            return None

        return ComplexityResult(
            complexity=complexity,  # type: ignore[arg-type]
            reasoning=data.get('reasoning', ''),
        )

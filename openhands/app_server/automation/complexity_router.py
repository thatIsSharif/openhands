"""Complexity router — maps Jira task complexity tiers to LLM model names.

Reads model overrides from environment variables so deployers can configure
which model handles each complexity tier without code changes.

Feature gate: all three env vars must be set for the router to activate.
If any is missing the caller should skip complexity analysis entirely.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ComplexityRouter:
    """Maps complexity tiers to model names loaded from environment."""

    complex_model: str | None
    medium_model: str | None
    low_model: str | None

    @classmethod
    def from_env(cls) -> 'ComplexityRouter':
        return cls(
            complex_model=os.getenv('OH_JIRA_COMPLEX_MODEL'),
            medium_model=os.getenv('OH_JIRA_MEDIUM_MODEL'),
            low_model=os.getenv('OH_JIRA_LOW_MODEL'),
        )

    @property
    def is_enabled(self) -> bool:
        """True when all three model env vars are configured."""
        return all([self.complex_model, self.medium_model, self.low_model])

    def resolve(self, complexity: str) -> str | None:
        """Return the model name for *complexity*, or ``None`` for unknown tiers."""
        return {
            'complex': self.complex_model,
            'medium': self.medium_model,
            'low': self.low_model,
        }.get(complexity)

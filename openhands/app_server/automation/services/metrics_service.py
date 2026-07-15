"""Metrics service for the automation platform.

Fetches and formats token usage metrics from the agent server.
Replaces the inline logic previously in jira_webhook_router.py.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from openhands.app_server.utils.logger import openhands_logger as logger


def _format_ts(ts: str | None) -> str:
    """Format an ISO-8601 timestamp for display."""
    if not ts:
        return 'N/A'
    try:
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        return dt.strftime('%Y-%m-%d %H:%M:%S UTC')
    except (ValueError, AttributeError):
        return ts


class MetricsService:
    """Fetches and formats live token usage metrics from the agent server."""

    TOKEN_USAGE_MARKER = 'OpenHands Automation Complete'

    async def fetch_live_metrics(
        self,
        agent_server_url: str,
        conversation_id: str,
        session_api_key: str,
    ) -> dict:
        """Fetch live conversation metrics from the agent server API.

        Returns a dict with keys: accumulated_cost, model_name, prompt_tokens,
        completion_tokens, cache_read_tokens, cache_write_tokens,
        reasoning_tokens.

        On failure returns an empty dict (callers should fall back gracefully).
        """
        from openhands.app_server.utils.docker_utils import (
            replace_localhost_hostname_for_docker,
        )

        url = replace_localhost_hostname_for_docker(agent_server_url)
        url = f'{url}/api/conversations/{conversation_id}'

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url,
                    headers={'X-Session-API-Key': session_api_key},
                    timeout=10.0,
                )
            if resp.status_code != 200:
                return {}

            data = resp.json()
            stats = data.get('stats') or {}
            usage_to_metrics = stats.get('usage_to_metrics') or {}
            agent = usage_to_metrics.get('agent') or {}
            usage = agent.get('accumulated_token_usage') or {}

            return {
                'accumulated_cost': agent.get('accumulated_cost', 0.0),
                'model_name': agent.get('model_name', 'default'),
                'prompt_tokens': usage.get('prompt_tokens', 0),
                'completion_tokens': usage.get('completion_tokens', 0),
                'cache_read_tokens': usage.get('cache_read_tokens', 0),
                'cache_write_tokens': usage.get('cache_write_tokens', 0),
                'reasoning_tokens': usage.get('reasoning_tokens', 0),
                'created_at': data.get('created_at'),
                'updated_at': data.get('updated_at'),
            }
        except Exception:
            import traceback

            logger.error(
                f'[Automation] Error fetching live conversation '
                f'{conversation_id}: {traceback.format_exc()}'
            )
            return {}

    def build_token_usage_comment(
        self,
        accumulated_cost: float = 0.0,
        model_name: str = 'default',
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        max_budget: float | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> dict:
        """Build a token-usage comment as an ADF document.

        Returns an ADF dict ready to pass to JiraApiService.add_comment().
        """
        content: list[dict] = []

        # Header
        content.append({
            'type': 'heading',
            'attrs': {'level': 3},
            'content': [
                {
                    'type': 'emoji',
                    'attrs': {'shortName': ':dart:', 'text': '🎯'},
                },
                self._text(f'  {self.TOKEN_USAGE_MARKER}', bold=True),
            ],
        })

        # Cost + model summary line
        total_tokens = prompt_tokens + completion_tokens
        summary_content = [
            self._text('💰 Cost  '),
            self._text(f'${accumulated_cost:.6f}', bold=True),
            self._text('     🤖 Model  '),
            self._text(model_name, bold=True),
        ]
        content.append({'type': 'paragraph', 'content': summary_content})
        content.append({'type': 'rule'})

        # Token usage section
        if total_tokens > 0 or prompt_tokens > 0 or completion_tokens > 0:
            content.append({
                'type': 'paragraph',
                'content': [self._text('📊 Token Usage', bold=True)],
            })

            bullet_items = [
                ('Prompt tokens', f'{prompt_tokens:,}'),
                ('Completion tokens', f'{completion_tokens:,}'),
                ('Total tokens', f'{total_tokens:,}'),
            ]
            if cache_read_tokens:
                bullet_items.append(
                    ('Cache read tokens', f'{cache_read_tokens:,}')
                )
            if cache_write_tokens:
                bullet_items.append(
                    ('Cache write tokens', f'{cache_write_tokens:,}')
                )
            if reasoning_tokens:
                bullet_items.append(
                    ('Reasoning tokens', f'{reasoning_tokens:,}')
                )

            content.append({
                'type': 'bulletList',
                'content': [
                    {
                        'type': 'listItem',
                        'content': [
                            {
                                'type': 'paragraph',
                                'content': [
                                    self._text(f'{label}: ', color='#6B778C'),
                                    self._text(value, bold=True),
                                ],
                            }
                        ],
                    }
                    for label, value in bullet_items
                ],
            })

        # Budget usage
        if max_budget and max_budget > 0:
            pct = accumulated_cost / max_budget * 100
            budget_color = (
                '#DE350B' if pct >= 90 else '#FF8B00' if pct >= 70 else '#00875A'
            )
            content.append({'type': 'rule'})
            content.append(self._stat_paragraph(
                '📋 Budget Usage',
                f'${accumulated_cost:.4f} / ${max_budget:.4f}  ({pct:.1f}%)',
                value_color=budget_color,
            ))

        # Timestamps
        content.append({'type': 'rule'})
        content.append({
            'type': 'paragraph',
            'content': [
                self._text('⏱️ Created  ', color='#6B778C'),
                self._text(_format_ts(created_at), bold=True),
                self._text('     ⏱️ Updated  ', color='#6B778C'),
                self._text(_format_ts(updated_at), bold=True),
            ],
        })

        return {
            'type': 'doc',
            'version': 1,
            'content': content,
        }

    @staticmethod
    def _text(
        value: str, *, bold: bool = False, color: str | None = None
    ) -> dict:
        """Build an ADF text node, optionally bold and/or colored."""
        marks = []
        if bold:
            marks.append({'type': 'strong'})
        if color:
            marks.append({'type': 'textColor', 'attrs': {'color': color}})
        node: dict = {'type': 'text', 'text': value}
        if marks:
            node['marks'] = marks
        return node

    @staticmethod
    def _stat_paragraph(
        label: str, value: str, *, value_color: str | None = None
    ) -> dict:
        """A single 'Label: Value' line with muted label and bold value."""
        return {
            'type': 'paragraph',
            'content': [
                MetricsService._text(f'{label}: ', color='#6B778C'),
                MetricsService._text(value, bold=True, color=value_color),
            ],
        }

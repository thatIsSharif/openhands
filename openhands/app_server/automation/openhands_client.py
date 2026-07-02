from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

import httpx
from fastapi import Request

from openhands.agent_server.models import (
    SendMessageRequest,
    TextContent,
)
from openhands.app_server.app_conversation.app_conversation_models import (
    AppConversationStartRequest,
    AppConversationStartTaskStatus,
    ConversationTrigger,
)
from openhands.app_server.automation.budget_enforcement_processor import (
    BudgetEnforcementProcessor,
)
from openhands.app_server.automation.callback_processors import (
    AutomationEventCallbackProcessor,
)
from openhands.app_server.automation.execution_store import ExecutionStore
from openhands.app_server.config import (
    get_app_conversation_info_service,
    get_app_conversation_service,
    get_httpx_client,
    get_sandbox_service,
)
from openhands.app_server.integrations.service_types import ProviderType
from openhands.app_server.sandbox.sandbox_models import AGENT_SERVER
from openhands.app_server.utils.docker_utils import (
    replace_localhost_hostname_for_docker,
)
from openhands.app_server.utils.logger import (
    openhands_logger as logger,
)
from openhands.sdk.security import EnsembleSecurityAnalyzer
from openhands.sdk.security.confirmation_policy import ConfirmRisky
from openhands.sdk.security.defense_in_depth import (
    PatternSecurityAnalyzer,
    PolicyRailSecurityAnalyzer,
)

from .correlation import build_log_context


def _build_automation_ensemble() -> EnsembleSecurityAnalyzer:
    """Build the full EnsembleSecurityAnalyzer with all automation patterns.

    Reusable in the initial conversation creation and when re-registering
    the analyzer on a resumed conversation. Uses only SDK-native types so
    the agent server can deserialize the payload correctly.
    """
    from openhands.app_server.automation.automation_security_analyzer import (
        AUTOMATION_GIT_PATTERNS,
        AUTOMATION_GITHUB_PATTERNS,
        AUTOMATION_HIGH_PATTERNS,
    )

    return EnsembleSecurityAnalyzer(
        analyzers=[
            PolicyRailSecurityAnalyzer(),
            # SDK defaults only (rm -rf, curl|sh, eval, etc.)
            PatternSecurityAnalyzer(),
            # Automation-specific patterns only
            PatternSecurityAnalyzer(
                high_patterns=(
                    AUTOMATION_HIGH_PATTERNS
                    + AUTOMATION_GIT_PATTERNS
                    + AUTOMATION_GITHUB_PATTERNS
                ),
            ),
        ]
    )


@dataclass
class OpenHandsClient:
    """Creates automation conversations through the OSS service layer."""

    async def create_conversation(
        self,
        state,
        request: Request | None,
        prompt: str,
        title: str = '[Automation] Execution',
        execution_id: str | None = None,
        jira_issue_key: str | None = None,
        pr_number: int | None = None,
        repository: str | None = None,
        branch: str | None = None,
    ) -> str | None:
        # Look up execution record for task-level rate limits
        max_iterations: int | None = None
        max_budget: float | None = None
        if execution_id:
            store = ExecutionStore()
            execution_record = await store.get_execution(execution_id)
            if execution_record:
                max_iterations = execution_record.max_iterations
                max_budget = execution_record.max_budget

        # Build the processor list
        processors = [AutomationEventCallbackProcessor()]

        # Register budget enforcement if a max_budget is configured
        if max_budget is not None and max_budget > 0:
            processors.append(BudgetEnforcementProcessor())

        start_request = AppConversationStartRequest(
            trigger=ConversationTrigger.AUTOMATION,
            title=title,
            selected_repository=repository,
            selected_branch=branch or 'main',
            git_provider=ProviderType.GITHUB,
            initial_message=SendMessageRequest(
                content=[
                    TextContent(
                        text=prompt,
                    )
                ]
            ),
            max_iterations=max_iterations,
            max_budget_per_task=max_budget,
            # Layer 2: Security analyzer active from first agent step
            security_analyzer='automation',
            # Enable confirmation mode so that risky actions (e.g. git branch -D)
            # trigger WAITING_FOR_CONFIRMATION instead of executing. A background
            # monitor auto-rejects by calling respond_to_confirmation + /run.
            confirmation_mode=True,
            processors=processors,
            jira_issue_key=jira_issue_key,
        )

        async with get_app_conversation_service(
            state,
            request,
        ) as service:
            if service is None:
                logger.error(
                    '[Automation] AppConversationService not available',
                    extra=build_log_context(
                        execution_id=execution_id or '',
                        jira_issue_key=jira_issue_key,
                    ),
                )
                return None

            try:
                conversation_id = None

                async for task in service.start_app_conversation(start_request):
                    logger.info(f'[Automation] Start task status: {task.status}')

                    if task.status == AppConversationStartTaskStatus.READY:
                        conversation_id = str(task.app_conversation_id)
                        break

                    if task.status == AppConversationStartTaskStatus.ERROR:
                        logger.error(
                            f'[Automation] Conversation startup failed: {task.detail}'
                        )
                        return None

                if not conversation_id:
                    logger.error(
                        '[Automation] Conversation startup '
                        'finished without READY status'
                    )
                    return None

                logger.info(
                    f'[Automation] Created conversation {conversation_id}',
                    extra=build_log_context(
                        execution_id=execution_id or '',
                        conversation_id=conversation_id,
                        jira_issue_key=jira_issue_key,
                        pr_number=pr_number,
                        repository=repository,
                    ),
                )

                # Re-register security analyzer as a follow-up.
                # SDK-native analyzers (PatternSecurityAnalyzer,
                # PolicyRailSecurityAnalyzer) are already active from the initial
                # POST /api/conversations. This replaces the conversation's analyzer
                # with a new Ensemble that includes the automation-specific patterns
                # (destructive commands, prod access, dangerous git ops), ensuring
                # they are active even if the initial POST missed them.
                await self._add_automation_security_analyzer(
                    state=state,
                    request=request,
                    conversation_id=conversation_id,
                    execution_id=execution_id,
                    jira_issue_key=jira_issue_key,
                )

                return conversation_id

            except Exception:
                import traceback

                logger.error(traceback.format_exc())
                return None

    async def _auto_reject_monitor(
        self,
        agent_server_url: str,
        session_api_key: str,
        conversation_id: str,
        execution_id: str | None = None,
        jira_issue_key: str | None = None,
    ) -> None:
        """Background task: poll conversation status and auto-reject dangerous actions.

        The security analyzer + ConfirmRisky policy causes the agent to enter
        WAITING_FOR_CONFIRMATION when it tries a HIGH-risk action (e.g. ``git
        branch -D main``). Since there is no user watching the UI in automation,
        this monitor polls the conversation status and auto-rejects on behalf
        of the user:

        1. POST ``/respond_to_confirmation`` with ``accept=false`` — the agent
           emits a ``UserRejectObservation`` and the dangerous action is NEVER
           executed.
        2. POST ``/{conversation_id}/run`` — restarts the agent loop so it
           continues to its next step.

        The monitor runs until the conversation reaches a terminal state
        (FINISHED, ERROR, STUCK) or the maximum poll time is exceeded.
        """
        base = agent_server_url.rstrip('/')
        headers = {'X-Session-API-Key': session_api_key}
        log_ctx = build_log_context(
            execution_id=execution_id or '',
            conversation_id=conversation_id,
            jira_issue_key=jira_issue_key,
        )

        POLL_INTERVAL = 2.0  # seconds between status checks
        MAX_POLLS = 1800  # 1 hour max (2s * 1800 = 3600s)

        # Track whether we've rejected and still need to call /run.
        # This handles race conditions where the rejection succeeded but
        # the /run call failed — we'll retry on the next cycle.
        needs_run = False

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                for _ in range(MAX_POLLS):
                    try:
                        resp = await client.get(
                            f'{base}/api/conversations/{conversation_id}',
                            headers=headers,
                        )
                        if resp.status_code != 200:
                            await asyncio.sleep(POLL_INTERVAL)
                            continue

                        info = resp.json()
                        # The ConversationInfo model has execution_status field
                        exec_status = info.get('execution_status', '') or ''

                    except Exception:
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                    # Terminal state → stop monitoring
                    if exec_status in ('finished', 'error', 'stuck'):
                        break

                    # Auto-reject dangerous actions
                    if exec_status == 'waiting_for_confirmation':
                        reject_ok = await self._auto_reject_once(
                            client, base, conversation_id, headers, log_ctx
                        )
                        if reject_ok:
                            needs_run = True

                    # If we've rejected and need to restart the agent loop,
                    # call /run. Retries on every cycle until it succeeds or
                    # the conversation reaches a terminal state.
                    if needs_run:
                        try:
                            run_resp = await client.post(
                                f'{base}/api/conversations/'
                                f'{conversation_id}/run',
                                headers=headers,
                            )
                            if run_resp.status_code in (200, 409):
                                # 200 = started, 409 = already running — both OK
                                needs_run = False
                            else:
                                logger.warning(
                                    '[Automation] /run returned %s for %s',
                                    run_resp.status_code,
                                    conversation_id,
                                    extra=log_ctx,
                                )
                        except Exception as e:
                            logger.warning(
                                '[Automation] /run failed for %s: %s',
                                conversation_id, e, extra=log_ctx,
                            )

                    await asyncio.sleep(POLL_INTERVAL)

                logger.info(
                    '[Security] Auto-reject monitor stopped for conversation %s',
                    conversation_id, extra=log_ctx,
                )

        except Exception as e:
            logger.warning(
                '[Security] Auto-reject monitor error for conversation %s: %s',
                conversation_id, e, extra=log_ctx,
            )

    async def _auto_reject_once(
        self,
        client: httpx.AsyncClient,
        base: str,
        conversation_id: str,
        headers: dict,
        log_ctx: dict,
    ) -> bool:
        """Reject pending actions via respond_to_confirmation.

        Returns True on success, False on failure.
        """
        try:
            resp = await client.post(
                f'{base}/api/conversations/'
                f'{conversation_id}/events/respond_to_confirmation',
                json={
                    'accept': False,
                    'reason': (
                        'Auto-rejected by automation security policy — '
                        'no user available to confirm.'
                    ),
                },
                headers=headers,
            )
            resp.raise_for_status()

            logger.info(
                '[Security] Auto-rejected dangerous actions for conversation %s',
                conversation_id, extra=log_ctx,
            )
            return True

        except Exception as e:
            logger.warning(
                '[Security] Auto-reject call failed for conversation %s: %s',
                conversation_id, e, extra=log_ctx,
            )
            return False

    async def _setup_security_for_conversation(
        self,
        agent_server_url: str,
        session_api_key: str,
        conversation_id: str,
        execution_id: str | None = None,
        jira_issue_key: str | None = None,
    ) -> None:
        """POST security analyzer + ConfirmRisky, then start auto-reject monitor.

        This is the single place where security configuration is applied to
        a running conversation. It is called both at initial creation (from
        ``_add_automation_security_analyzer``) and when resuming via follow-up
        Jira comments or GitHub reviews.

        Steps
        -----
        1. POST ``ConfirmRisky`` as the confirmation policy so dangerous
           actions enter ``WAITING_FOR_CONFIRMATION``.
        2. POST the ``EnsembleSecurityAnalyzer`` with all automation patterns,
           replacing whatever analyzer the conversation may already have.
        3. Start the ``_auto_reject_monitor`` background task to auto-reject
           on behalf of the automation (no user available to approve).
        """
        base = agent_server_url.rstrip('/')
        headers = {'X-Session-API-Key': session_api_key}
        log_ctx = build_log_context(
            execution_id=execution_id or '',
            conversation_id=conversation_id,
            jira_issue_key=jira_issue_key,
        )

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # 1. Set confirmation policy to ConfirmRisky
                resp = await client.post(
                    f'{base}/api/conversations/'
                    f'{conversation_id}/confirmation_policy',
                    json={'policy': ConfirmRisky().model_dump()},
                    headers=headers,
                )
                resp.raise_for_status()
                logger.info(
                    '[Security] ConfirmRisky set for conversation %s',
                    conversation_id,
                    extra=log_ctx,
                )

                # 2. Set security analyzer with automation patterns
                ensemble = _build_automation_ensemble()
                resp = await client.post(
                    f'{base}/api/conversations/'
                    f'{conversation_id}/security_analyzer',
                    json={'security_analyzer': ensemble.model_dump()},
                    headers=headers,
                )
                resp.raise_for_status()
                logger.info(
                    '[Security] EnsembleSecurityAnalyzer set for conversation %s',
                    conversation_id,
                    extra=log_ctx,
                )

            # 3. Start background monitor (uses its own client)
            asyncio.create_task(
                self._auto_reject_monitor(
                    agent_server_url=agent_server_url,
                    session_api_key=session_api_key,
                    conversation_id=conversation_id,
                    execution_id=execution_id,
                    jira_issue_key=jira_issue_key,
                )
            )

            logger.info(
                '[Security] Auto-reject monitor started for conversation %s',
                conversation_id,
                extra=log_ctx,
            )

        except Exception as e:
            logger.warning(
                '[Security] Failed to setup security for conversation %s: %s',
                conversation_id,
                e,
                extra=log_ctx,
            )

    async def _add_automation_security_analyzer(
        self,
        state,
        request: Request | None,
        conversation_id: str,
        execution_id: str | None = None,
        jira_issue_key: str | None = None,
    ) -> None:
        """Discover sandbox and apply security configuration.

        This is a convenience wrapper used at *initial creation* time.
        It retries sandbox discovery (the sandbox may not be ready yet)
        and then delegates to ``_setup_security_for_conversation`` which
        POSTs ``ConfirmRisky`` + the automation ensemble and starts the
        auto-reject monitor.

        For follow-up comments and reviews the webhook routers call
        ``_setup_security_for_conversation`` directly (the sandbox info
        is already available).
        """
        log_ctx = build_log_context(
            execution_id=execution_id or '',
            conversation_id=conversation_id,
            jira_issue_key=jira_issue_key,
        )

        # Retry sandbox discovery — the sandbox may not be ready yet since
        # it's created async with the conversation.
        agent_server_url: str | None = None
        session_api_key: str | None = None

        for _ in range(10):
            try:
                async with get_app_conversation_info_service(
                    state, request
                ) as info_service:
                    conv_info = await info_service.get_app_conversation_info(
                        UUID(conversation_id)
                    )
                    if not conv_info or not conv_info.sandbox_id:
                        await asyncio.sleep(1)
                        continue

                async with get_sandbox_service(state, request) as sandbox_service:
                    sandbox = await sandbox_service.get_sandbox(conv_info.sandbox_id)
                    if sandbox and sandbox.exposed_urls:
                        for exposed_url in sandbox.exposed_urls:
                            if exposed_url.name == AGENT_SERVER:
                                agent_server_url = replace_localhost_hostname_for_docker(
                                    exposed_url.url
                                )
                                break
                        session_api_key = sandbox.session_api_key

                if agent_server_url and session_api_key:
                    break
                await asyncio.sleep(1)
            except Exception:
                await asyncio.sleep(1)

        if not agent_server_url or not session_api_key:
            logger.warning(
                '[Security] Cannot discover sandbox for conversation %s '
                'after 10 retries — security NOT applied.',
                conversation_id,
                extra=log_ctx,
            )
            return

        await self._setup_security_for_conversation(
            agent_server_url=agent_server_url,
            session_api_key=session_api_key,
            conversation_id=conversation_id,
            execution_id=execution_id,
            jira_issue_key=jira_issue_key,
        )

import json
from typing import Any, cast

import httpx
from pydantic import SecretStr

from openhands.app_server.integrations.protocols.http_client import HTTPClient
from openhands.app_server.integrations.service_types import (
    BaseGitService,
    RequestMethod,
    UnknownException,
    User,
)
from openhands.app_server.utils.http_session import httpx_verify_option
from openhands.app_server.utils.logger import openhands_logger as logger


class GitHubMixinBase(BaseGitService, HTTPClient):
    """
    Declares common attributes and method signatures used across mixins.
    """

    BASE_URL: str
    GRAPHQL_URL: str

    # Optional repository context for GitHub App token resolution.
    selected_repository: str | None = None

    @staticmethod
    def _resolve_primary_email(emails: list[dict]) -> str | None:
        """Find the primary verified email from a list of GitHub email objects.

        GitHub's /user/emails endpoint returns a list of dicts, each with
        'email', 'primary', and 'verified' keys. This selects the one marked
        as both primary and verified — the email the user considers canonical.
        """
        for entry in emails:
            if entry.get('primary') and entry.get('verified'):
                return entry.get('email')
        return None

    async def _get_headers(self) -> dict:
        """Retrieve a GitHub App installation token for headers.

        Uses GitHub App exclusively.  Raises if GitHub App is not
        configured — there is no PAT fallback.
        """
        gh_app_token = await self._resolve_github_app_token()
        if not gh_app_token:
            from openhands.app_server.utils.github_app import (
                GitHubAppNotConfiguredError,
            )

            raise GitHubAppNotConfiguredError(
                'GitHub App not configured. '
                'Set GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY, '
                'and GITHUB_APP_INSTALLATION_ID.'
            )

        return {
            'Authorization': f'Bearer {gh_app_token}',
            'Accept': 'application/vnd.github.v3+json',
        }

    async def _resolve_github_app_token(self) -> str | None:
        """Resolve a GitHub App installation token.

        Reads ``GITHUB_APP_INSTALLATION_ID`` from the environment.

        Returns None if GitHub App is not configured.
        """
        from openhands.app_server.utils.github_app import (
            GitHubAppTokenManager,
        )

        if not GitHubAppTokenManager.is_available():
            return None

        try:
            return GitHubAppTokenManager.get_token_for_installation()
        except Exception:
            logger.exception('Failed to resolve GitHub App token')
            return None

    async def get_latest_token(self) -> SecretStr | None:  # type: ignore[override]
        """Satisfy the abstract method — PAT-based refresh is not supported."""
        return None

    async def _make_request(
        self,
        url: str,
        params: dict | None = None,
        method: RequestMethod = RequestMethod.GET,
    ) -> tuple[Any, dict]:  # type: ignore[override]
        try:
            async with httpx.AsyncClient(verify=httpx_verify_option()) as client:
                github_headers = await self._get_headers()

                # Make initial request
                response = await self.execute_request(
                    client=client,
                    url=url,
                    headers=github_headers,
                    params=params,
                    method=method,
                )

                # Handle token refresh if needed
                if self.refresh and self._has_token_expired(response.status_code):
                    # Re-resolve a fresh GitHub App installation token
                    github_headers = await self._get_headers()
                    response = await self.execute_request(
                        client=client,
                        url=url,
                        headers=github_headers,
                        params=params,
                        method=method,
                    )

                response.raise_for_status()
                headers: dict = {}
                if 'Link' in response.headers:
                    headers['Link'] = response.headers['Link']

                return response.json(), headers

        except httpx.HTTPStatusError as e:
            raise self.handle_http_status_error(e)
        except httpx.HTTPError as e:
            raise self.handle_http_error(e)

    async def execute_graphql_query(
        self, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(verify=httpx_verify_option()) as client:
                github_headers = await self._get_headers()

                response = await client.post(
                    self.GRAPHQL_URL,
                    headers=github_headers,
                    json={'query': query, 'variables': variables},
                )
                response.raise_for_status()

                result = response.json()
                if 'errors' in result:
                    raise UnknownException(
                        f'GraphQL query error: {json.dumps(result["errors"])}'
                    )

                return dict(result)

        except httpx.HTTPStatusError as e:
            raise self.handle_http_status_error(e)
        except httpx.HTTPError as e:
            raise self.handle_http_error(e)

    async def get_user_emails(self) -> list[dict]:
        """Fetch the authenticated user's email addresses from GitHub.

        NOTE: With GitHub App installation tokens this endpoint will
        return 403. Returns an empty list so callers don't break.
        """
        url = f'{self.BASE_URL}/user/emails'
        try:
            response, _ = await self._make_request(url)
            return response
        except Exception:
            return []

    async def verify_access(self) -> bool:
        url = f'{self.BASE_URL}'
        await self._make_request(url)
        return True

    async def get_user(self):
        """Fetch the authenticated GitHub user's information.

        With GitHub App installation tokens, GET /user returns 403
        (installation tokens cannot authenticate user-scoped endpoints).
        When that happens, returns a minimal ``User`` using the owner
        from ``selected_repository`` if available, so callers in
        ``features.py`` and ``repos.py`` can still derive a login name
        for GraphQL queries and search filters.

        The ``provider.py`` flow also catches the error and falls
        through to other configured providers (GitLab, Bitbucket, etc.).
        """
        url = f'{self.BASE_URL}/user'
        try:
            response, _ = await self._make_request(url)
        except Exception:
            logger.warning(
                'github:get_user:failed_with_installation_token',
                exc_info=True,
            )
            # Return a minimal user derived from the selected repository
            if self.selected_repository:
                login, _, _ = self.selected_repository.partition('/')
                return User(
                    id='',
                    login=login,
                    avatar_url='',
                    company='',
                    name=login,
                    email=None,
                )
            return User(
                id='',
                login='',
                avatar_url='',
                company='',
                name='',
                email=None,
            )

        email = response.get('email')
        if email is None:
            try:
                emails = await self.get_user_emails()
                email = self._resolve_primary_email(emails)
            except Exception:
                logger.warning(
                    'github:get_user:email_fallback_failed',
                    exc_info=True,
                )

        return User(
            id=str(response.get('id', '')),
            login=cast(str, response.get('login') or ''),
            avatar_url=cast(str, response.get('avatar_url') or ''),
            company=response.get('company'),
            name=response.get('name'),
            email=email,
        )

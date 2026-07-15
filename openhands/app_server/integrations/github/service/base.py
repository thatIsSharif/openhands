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

        Uses GitHub App exclusively. Raises if GitHub App is not
        configured — there is no PAT fallback.
        """
        token = await self._resolve_github_app_token()
        if not token:
            from openhands.app_server.utils.github_app import (
                GitHubAppNotConfiguredError,
            )

            raise GitHubAppNotConfiguredError(
                'GitHub App not configured. '
                'Set GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY.'
            )

        return {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github.v3+json',
        }

    async def _resolve_github_app_token(self) -> str | None:
        """Try to resolve a GitHub App installation token.

        Uses ``selected_repository`` if set, otherwise falls back to the
        default installation ID from the environment.

        Returns None if GitHub App is not configured.
        """
        from openhands.app_server.utils.github_app import (
            GitHubAppTokenManager,
        )

        if not GitHubAppTokenManager.is_available():
            return None

        try:
            if self.selected_repository:
                owner, _, repo = self.selected_repository.partition('/')
                return GitHubAppTokenManager.get_token_for_repository(
                    owner, repo
                )

            return GitHubAppTokenManager.get_token_for_installation()
        except Exception:
            logger.exception('Failed to resolve GitHub App token')
            return None

    async def get_latest_token(self) -> SecretStr | None:
        """Satisfy the abstract method from ``HTTPClient``.

        Returns ``None`` because PAT-based token refresh is not
        supported — only GitHub App installation tokens are used.
        """
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
                    # and retry the request
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

        NOTE: This endpoint requires a user PAT.  With GitHub App
        installation tokens it will fail with 403.  The calling code
        in ``provider.py`` handles this gracefully by falling through
        to other configured providers.
        """
        url = f'{self.BASE_URL}/user/emails'
        response, _ = await self._make_request(url)
        return response

    async def verify_access(self) -> bool:
        url = f'{self.BASE_URL}'
        await self._make_request(url)
        return True

    async def get_user(self):
        """Fetch the authenticated GitHub user.

        NOTE: This endpoint requires a user PAT.  With GitHub App
        installation tokens it will fail with 403.  The calling code
        in ``provider.py`` handles this gracefully by falling through
        to other configured providers.
        """
        url = f'{self.BASE_URL}/user'
        response, _ = await self._make_request(url)

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

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
    # When set, _get_headers prefers a GitHub App installation token
    # over the user PAT.
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

    async def _get_headers(
        self,
        *,
        use_github_app: bool = True,
    ) -> dict:
        """Retrieve the GH Token from settings store to construct the headers.

        When ``use_github_app`` is True (default) and the GitHub App is
        configured, returns an installation access token.  Set
        ``use_github_app=False`` for user-scoped endpoints (``/user``,
        ``/user/emails``) that installation tokens cannot authenticate.

        Falls back to the user PAT (``self.token``) when the GitHub App
        is unavailable or ``use_github_app`` is False.
        """
        if use_github_app:
            gh_app_token = await self._resolve_github_app_token()
            if gh_app_token:
                return {
                    'Authorization': f'Bearer {gh_app_token}',
                    'Accept': 'application/vnd.github.v3+json',
                }

        # Fall back to the user PAT
        if not self.token:
            latest_token = await self.get_latest_token()
            if latest_token:
                self.token = latest_token

        return {
            'Authorization': f'Bearer {self.token.get_secret_value() if self.token else ""}',
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
            import logging as _logging

            _logging.getLogger(__name__).exception(
                'Failed to resolve GitHub App token, falling back to PAT'
            )
            return None

    async def get_latest_token(self) -> SecretStr | None:  # type: ignore[override]
        return self.token

    async def _make_request(
        self,
        url: str,
        params: dict | None = None,
        method: RequestMethod = RequestMethod.GET,
        *,
        use_github_app: bool = True,
    ) -> tuple[Any, dict]:  # type: ignore[override]
        try:
            async with httpx.AsyncClient(verify=httpx_verify_option()) as client:
                github_headers = await self._get_headers(
                    use_github_app=use_github_app,
                )

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
                    await self.get_latest_token()
                    github_headers = await self._get_headers(
                        use_github_app=use_github_app,
                    )
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

        Calls GET /user/emails which returns a list of email objects, each
        containing 'email', 'primary', 'verified', and 'visibility' fields.
        Requires the user:email OAuth scope.

        Note: uses ``use_github_app=False`` because installation tokens
        cannot authenticate user-scoped endpoints.
        """
        url = f'{self.BASE_URL}/user/emails'
        response, _ = await self._make_request(url, use_github_app=False)
        return response

    async def verify_access(self) -> bool:
        url = f'{self.BASE_URL}'
        await self._make_request(url)
        return True

    async def get_user(self):
        url = f'{self.BASE_URL}/user'
        response, _ = await self._make_request(url, use_github_app=False)

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

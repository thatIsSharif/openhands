"""GitHub App authentication manager.

Uses PyGithub's GithubIntegration to mint short-lived installation access
tokens instead of relying on long-lived personal access tokens (PATs).

Usage:
    from openhands.app_server.utils.github_app import GitHubAppTokenManager

    # Synchronous usage (e.g. utils/github.py)
    token = GitHubAppTokenManager.get_token_for_repository("owner", "repo")
    token = GitHubAppTokenManager.get_installation_token(123456)

    # Check if GitHub App is configured
    if GitHubAppTokenManager.is_available():
        ...

Requires env vars: GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY (or
GITHUB_APP_PRIVATE_KEY_PATH). Optionally GITHUB_APP_INSTALLATION_ID
for a default installation.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from github import GithubIntegration

logger = logging.getLogger(__name__)

# In-memory token cache: {cache_key: {"token": str, "expires_at": datetime}}
_token_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


def _load_private_key() -> str:
    """Load the GitHub App private key from the environment.

    Checks ``GITHUB_APP_PRIVATE_KEY`` first (inline PEM), then
    ``GITHUB_APP_PRIVATE_KEY_PATH`` (file path).
    """
    key = os.environ.get('GITHUB_APP_PRIVATE_KEY', '')
    if key:
        return key
    key_path = os.environ.get('GITHUB_APP_PRIVATE_KEY_PATH', '')
    if key_path:
        try:
            with open(key_path) as f:
                return f.read()
        except OSError as e:
            logger.warning('Failed to read GITHUB_APP_PRIVATE_KEY_PATH: %s', e)
    return ''


def _get_default_installation_id() -> int | None:
    """Return the default installation ID from env, if set."""
    val = os.environ.get('GITHUB_APP_INSTALLATION_ID', '')
    if val:
        try:
            return int(val)
        except ValueError:
            logger.warning('Invalid GITHUB_APP_INSTALLATION_ID: %s', val)
    return None


class GitHubAppNotConfiguredError(RuntimeError):
    """Raised when GitHub App credentials are not available."""


class GitHubAppTokenManager:
    """Manages GitHub App installation access tokens with caching & auto-refresh.

    Tokens are cached in memory and refreshed automatically when they are
    within 10 minutes of expiry.  Thread-safe.
    """

    _integration: GithubIntegration | None = None
    _integration_lock = threading.Lock()

    # ── Initialization ─────────────────────────────────────────────

    @classmethod
    def _get_integration(cls) -> GithubIntegration | None:
        """Lazy-init and cache the ``GithubIntegration`` instance."""
        if cls._integration is not None:
            return cls._integration

        with cls._integration_lock:
            # Double-checked locking
            if cls._integration is not None:
                return cls._integration

            app_id = os.environ.get('GITHUB_APP_ID')
            private_key = _load_private_key()

            if not app_id or not private_key:
                return None

            try:
                cls._integration = GithubIntegration(
                    integration_id=int(app_id),
                    private_key=private_key,
                )
            except Exception:
                logger.exception(
                    'Failed to initialize GithubIntegration '
                    '(check GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY)'
                )
                return None

        return cls._integration

    @classmethod
    def is_available(cls) -> bool:
        """Return True if GitHub App credentials are configured."""
        return cls._get_integration() is not None

    # ── Token retrieval ────────────────────────────────────────────

    @classmethod
    def get_token_for_installation(
        cls, installation_id: int | None = None
    ) -> str:
        """Get a token for an installation, using a default if none given.

        Args:
            installation_id: The GitHub App installation ID. If None,
                uses ``GITHUB_APP_INSTALLATION_ID`` from the environment.

        Returns:
            A valid installation access token string.

        Raises:
            GitHubAppNotConfiguredError: If GitHub App is not configured.
            RuntimeError: If no installation ID can be resolved.
        """
        if installation_id is None:
            installation_id = _get_default_installation_id()

        if installation_id is None:
            raise RuntimeError(
                'No installation ID available. Set GITHUB_APP_INSTALLATION_ID '
                'or pass an installation_id explicitly.'
            )

        return cls.get_installation_token(installation_id)

    @classmethod
    def get_installation_token(cls, installation_id: int) -> str:
        """Get a cached installation token, fetching or refreshing if needed.

        Args:
            installation_id: The GitHub App installation ID.

        Returns:
            A valid installation access token string.

        Raises:
            GitHubAppNotConfiguredError: If GitHub App is not configured.
            RuntimeError: If the token exchange fails.
        """
        integration = cls._get_integration()
        if not integration:
            raise GitHubAppNotConfiguredError(
                'GitHub App not configured. '
                'Set GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY.'
            )

        cache_key = f'inst:{installation_id}'

        # Check cache
        with _cache_lock:
            cached = _token_cache.get(cache_key)
            if cached and cls._is_token_fresh(cached['expires_at']):
                return cached['token']

        # Fetch new token
        auth = integration.get_access_token(installation_id)
        token = auth.token
        expires_at = cls._normalise_expiry(auth.expires_at)

        with _cache_lock:
            _token_cache[cache_key] = {
                'token': token,
                'expires_at': expires_at,
            }

        return token

    @classmethod
    def get_token_for_repository(cls, owner: str, repo: str) -> str:
        """Resolve the installation for a repository and return a token.

        Args:
            owner: Repository owner (user or org).
            repo: Repository name.

        Returns:
            A valid installation access token string.

        Raises:
            GitHubAppNotConfiguredError: If GitHub App is not configured.
            RuntimeError: If the installation cannot be resolved or
                the token exchange fails.
        """
        integration = cls._get_integration()
        if not integration:
            raise GitHubAppNotConfiguredError(
                'GitHub App not configured. '
                'Set GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY.'
            )

        cache_key = f'repo:{owner}/{repo}'

        # Check repo-level cache
        with _cache_lock:
            cached = _token_cache.get(cache_key)
            if cached and cls._is_token_fresh(cached['expires_at']):
                return cached['token']

        # Resolve installation and get token
        try:
            installation = integration.get_repo_installation(owner, repo)
            token = cls.get_installation_token(installation.id)
        except Exception:
            # Fallback: try default installation ID
            default_id = _get_default_installation_id()
            if default_id is not None:
                logger.info(
                    'Falling back to default installation %s for %s/%s',
                    default_id, owner, repo,
                )
                token = cls.get_installation_token(default_id)
            else:
                raise

        return token

    # ── Cache helpers ──────────────────────────────────────────────

    @classmethod
    def refresh_token(cls, owner: str, repo: str) -> str:
        """Force-refresh a cached token (used after 401/403).

        Clears the cached entry for the given repository, then fetches
        a fresh token.

        Args:
            owner: Repository owner.
            repo: Repository name.

        Returns:
            A fresh installation access token.
        """
        cache_key = f'repo:{owner}/{repo}'
        with _cache_lock:
            stale = _token_cache.pop(cache_key, None)
            # Also clear the underlying installation-level cache entry
            if stale:
                for key in list(_token_cache.keys()):
                    entry = _token_cache[key]
                    if entry.get('token') == stale.get('token'):
                        del _token_cache[key]
                        break

        return cls.get_token_for_repository(owner, repo)

    @classmethod
    def get_cached_token(cls, owner: str, repo: str) -> str | None:
        """Get the currently cached token without fetching (best-effort).

        Returns None if no valid cached token exists.
        """
        cache_key = f'repo:{owner}/{repo}'
        with _cache_lock:
            cached = _token_cache.get(cache_key)
            if cached and cls._is_token_fresh(cached['expires_at']):
                return cached['token']
        return None

    # ── Internals ──────────────────────────────────────────────────

    @staticmethod
    def _is_token_fresh(expires_at: datetime) -> bool:
        """Return True if the token has more than 10 minutes until expiry."""
        return datetime.now(timezone.utc) < expires_at - timedelta(minutes=10)

    @staticmethod
    def _normalise_expiry(dt: datetime) -> datetime:
        """Ensure the expiry datetime is timezone-aware (UTC)."""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

"""Reddit application-only OAuth token provider (``client_credentials`` grant).

Reddit blocks the unauthenticated ``.json`` endpoint (HTTP 403/429 even from residential IPs).
Authenticated requests to ``oauth.reddit.com`` need a Bearer token from a free registered app
(https://www.reddit.com/prefs/apps → "create app" → script or web app). This fetches and caches an
**application-only** token (no Reddit account needed — read access to public listings). A
descriptive ``User-Agent`` is mandatory per Reddit's API rules.
"""

from __future__ import annotations

import time

import httpx

from scrapeforge.exceptions import ScrapeForgeError

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"  # noqa: S105 (a URL, not a secret)
_EXPIRY_BUFFER_S = 60.0  # refresh a minute early so a token never expires mid-request


class RedditAuthError(ScrapeForgeError):
    """The Reddit OAuth token request failed (bad credentials, rate limit, etc.)."""


class RedditAuth:
    """Fetches + caches an application-only Reddit OAuth Bearer token."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        user_agent: str,
        timeout: float = 30.0,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._user_agent = user_agent
        self._timeout = timeout
        self._token: str | None = None
        self._expires_at: float = 0.0

    async def token(self) -> str:
        """Return a valid Bearer token, fetching + caching (until just before expiry) on demand."""
        if self._token is not None and time.monotonic() < self._expires_at:
            return self._token

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                _TOKEN_URL,
                auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials"},
                headers={"User-Agent": self._user_agent},
            )

        if resp.status_code != 200:
            raise RedditAuthError(
                f"Reddit token request failed: HTTP {resp.status_code}. Check REDDIT_CLIENT_ID / "
                "REDDIT_CLIENT_SECRET (a 'script' or 'web app' at https://www.reddit.com/prefs/apps)."
            )
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise RedditAuthError("Reddit token response contained no access_token.")

        self._token = token
        self._expires_at = time.monotonic() + float(data.get("expires_in", 3600)) - _EXPIRY_BUFFER_S
        return token

"""RedditAuth: app-only OAuth token fetch + caching (respx; no network)."""

from __future__ import annotations

import httpx
import pytest
import respx

from scrapeforge.scrapers.community.reddit_auth import RedditAuth, RedditAuthError

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"


@respx.mock
async def test_fetches_and_caches_token() -> None:
    route = respx.post(_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok123", "expires_in": 3600})
    )
    auth = RedditAuth(client_id="id", client_secret="sec", user_agent="ua/0.1")
    t1 = await auth.token()
    t2 = await auth.token()
    assert t1 == "tok123" and t2 == "tok123"
    assert route.call_count == 1  # cached — the second call does not re-POST


@respx.mock
async def test_raises_on_auth_failure() -> None:
    respx.post(_TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "invalid_grant"}))
    auth = RedditAuth(client_id="bad", client_secret="bad", user_agent="ua/0.1")
    with pytest.raises(RedditAuthError):
        await auth.token()


@respx.mock
async def test_sends_basic_auth_and_user_agent() -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})

    respx.post(_TOKEN_URL).mock(side_effect=_handler)
    auth = RedditAuth(client_id="id", client_secret="sec", user_agent="myUA/0.1")
    await auth.token()
    assert captured["auth"].startswith("Basic ")  # HTTP Basic (client_id:secret)
    assert captured["ua"] == "myUA/0.1"  # Reddit requires a descriptive UA

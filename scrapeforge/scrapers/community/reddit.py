"""RedditScraper — Bucket 2 community scraper for reddit.com.

Site Playbook: docs/research/reddit.md.

Implementation notes
--------------------
- Uses the public ``.json`` API (no OAuth, no browser).
- Driven by ``curl_cffi`` with Chrome impersonation + matching ``BrowserProfile``
  to satisfy Reddit's UA-gating (playbook §4.2).
- Pagination follows the ``after`` / ``count`` cursor pattern (playbook §5).
- Phase-2 escalation to ``patchright`` (for ``/api/morechildren`` + shreddit SPA)
  is deferred.

Per-module settings fragment (Invariant #16 — do NOT add to core Settings)
--------------------------------------------------------------------------
``RedditSettings`` is a standalone ``pydantic_settings.BaseSettings`` fragment.
Core ``Settings`` is not touched.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from scrapeforge.core.models import Article, ScrapeResult
from scrapeforge.core.registry import register_scraper
from scrapeforge.exceptions import DriverError
from scrapeforge.scrapers.community._base import CommunityScraper
from scrapeforge.scrapers.community.reddit_auth import RedditAuth

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-module settings fragment (Invariant #16)
# ---------------------------------------------------------------------------


class RedditSettings(BaseSettings):
    """Per-module configuration for RedditScraper.

    All values can be overridden via environment variables or a ``.env`` file.
    Never appended to core ``Settings`` (SPEC.md Invariant #16).
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    REDDIT_USE_JSON_API: bool = Field(default=True)
    REDDIT_JSON_LIMIT: int = Field(default=100)
    # --- OAuth (Reddit blocks the anonymous .json endpoint; oauth.reddit.com needs a token) ---
    REDDIT_CLIENT_ID: str = Field(default="")  # from https://www.reddit.com/prefs/apps
    REDDIT_CLIENT_SECRET: str = Field(default="")  # secret; .env only
    REDDIT_USER_AGENT: str = Field(default="scrapeforge/0.1 (investing news aggregator)")
    REDDIT_REQUEST_TIMEOUT: float = Field(default=30.0)

    def oauth_enabled(self) -> bool:
        """True when both OAuth credentials are present → use authenticated oauth.reddit.com."""
        return bool(self.REDDIT_CLIENT_ID and self.REDDIT_CLIENT_SECRET)


# ---------------------------------------------------------------------------
# RedditScraper
# ---------------------------------------------------------------------------

_BASE_URL = "https://www.reddit.com"
_OAUTH_BASE = "https://oauth.reddit.com"


@register_scraper("reddit.com", "www.reddit.com")
class RedditScraper(CommunityScraper):
    """Scraper for reddit.com using the public JSON API.

    Public interface
    ----------------
    - ``scrape(url)``        — single Reddit POST url → one ``ScrapeResult``.
    - ``scrape_subreddit()`` — paginated listing → list of ``ScrapeResult``.

    The engine's ``RateLimiter`` handles inter-request politeness; this class
    does NOT sleep between pages.
    """

    DOMAINS: list[str] = ["reddit.com", "www.reddit.com"]
    DEFAULT_DRIVER: str = "curl_cffi"

    def __init__(
        self,
        bridge=None,  # noqa: ANN001
        proxy: str | None = None,
        max_concurrency: int = 5,
        auth: RedditAuth | None = None,
    ) -> None:
        """Build the scraper. When ``REDDIT_CLIENT_ID``/``REDDIT_CLIENT_SECRET`` are set (or *auth*
        is injected), authenticated ``oauth.reddit.com`` is used; otherwise the legacy anonymous
        ``.json`` path (now 403-blocked by Reddit) — kept only for back-compat / injected-bridge
        tests."""
        super().__init__(bridge=bridge, proxy=proxy, max_concurrency=max_concurrency)
        settings = RedditSettings()
        self._user_agent = settings.REDDIT_USER_AGENT
        self._request_timeout = settings.REDDIT_REQUEST_TIMEOUT
        if auth is not None:
            self._auth: RedditAuth | None = auth
        elif settings.oauth_enabled():
            self._auth = RedditAuth(
                client_id=settings.REDDIT_CLIENT_ID,
                client_secret=settings.REDDIT_CLIENT_SECRET,
                user_agent=settings.REDDIT_USER_AGENT,
                timeout=settings.REDDIT_REQUEST_TIMEOUT,
            )
        else:
            self._auth = None

    async def _fetch_listing(self, path: str) -> dict | list:
        """Fetch a Reddit listing JSON for *path* (e.g. ``r/investing/hot.json?limit=5``).

        Authenticated ``oauth.reddit.com`` (Bearer + descriptive UA) when OAuth is configured;
        otherwise the legacy anonymous ``www.reddit.com`` ``.json`` path via the bridge.

        Note: ``oauth.reddit.com`` returns JSON natively and does NOT use the ``.json`` suffix (a
        www.reddit.com convention), so it is stripped on the OAuth branch. These direct httpx GETs
        bypass the engine RateLimiter; app-only OAuth allows ~100 QPM and the daily ingest fetches a
        single page per subreddit (limit ≤ 100), so it stays well within budget.
        """
        if self._auth is None:
            return await self._fetch_json(f"{_BASE_URL}/{path}")
        token = await self._auth.token()
        oauth_path = path.replace(".json?", "?", 1).removesuffix(".json")  # bare path for oauth.*
        async with httpx.AsyncClient(timeout=self._request_timeout) as client:
            resp = await client.get(
                f"{_OAUTH_BASE}/{oauth_path}",
                headers={"Authorization": f"Bearer {token}", "User-Agent": self._user_agent},
            )
        if resp.status_code != 200:
            raise DriverError(f"Reddit OAuth GET /{path} -> HTTP {resp.status_code}")
        return resp.json()

    # ------------------------------------------------------------------
    # Field mapping (playbook §2.2)
    # ------------------------------------------------------------------

    def _parse_post(self, data: dict) -> Article:
        """Map a Reddit ``t3`` post data dict to an ``Article``.

        Field rules (playbook §2.2)
        ---------------------------
        - ``content``: ``selftext`` when non-empty (self-post); empty for link-posts.
        - ``link_url``: stored in ``metadata`` for link-posts (``is_self=False``).
        - ``publish_date``: ``created_utc`` (float epoch) → UTC-aware datetime.
        - ``url``: absolute URL = ``https://www.reddit.com`` + ``permalink``.
        """
        is_self: bool = bool(data.get("is_self"))
        selftext: str = data.get("selftext") or ""
        # Treat "[removed]" / "[deleted]" as empty.
        if selftext in ("[removed]", "[deleted]"):
            selftext = ""

        content = selftext if is_self else ""

        metadata: dict = {
            "source_domain": "reddit.com",
            "bucket": "community",
            "subreddit": data.get("subreddit"),
            "score": data.get("score"),
            "num_comments": data.get("num_comments"),
            "reddit_id": data.get("id"),
        }

        # Link-post: store the external destination URL.
        if not is_self or not selftext:
            external_url = data.get("url")
            # Only store if it's truly an external link (not the reddit permalink).
            if external_url and "reddit.com" not in external_url:
                metadata["link_url"] = external_url

        permalink: str = data.get("permalink", "")
        article_url = _BASE_URL + permalink if permalink else data.get("url", "")

        publish_date: datetime | None = None
        created_utc = data.get("created_utc")
        if created_utc is not None:
            publish_date = datetime.fromtimestamp(float(created_utc), tz=UTC)

        return Article(
            url=article_url,
            title=data.get("title") or "",
            content=content,
            author=data.get("author"),
            publish_date=publish_date,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Paginated subreddit scrape
    # ------------------------------------------------------------------

    async def scrape_subreddit(
        self,
        subreddit: str,
        limit: int = 100,
        sort: str = "new",
    ) -> list[ScrapeResult]:
        """Scrape up to *limit* posts from ``/r/{subreddit}/{sort}``.

        Pagination follows playbook §5:
        - Fetch pages of ≤ 100 posts.
        - Pass ``after`` cursor and running ``count`` on subsequent pages.
        - Stop when ``after`` is null, children are empty, or *limit* reached.

        Parameters
        ----------
        subreddit:
            Subreddit name (without ``r/`` prefix).
        limit:
            Maximum number of posts to return (default 100).
        sort:
            Listing sort ('new', 'hot', 'top', 'rising', 'controversial').

        Returns
        -------
        list[ScrapeResult]
            One ``ScrapeResult(status='success', article=...)`` per ``t3`` post.
            Truncated to *limit* items.
        """
        results: list[ScrapeResult] = []
        after: str | None = None
        count: int = 0

        while len(results) < limit:
            page_size = min(100, limit - len(results))
            path = f"r/{subreddit}/{sort}.json?limit={page_size}&count={count}"
            if after:
                path += f"&after={after}"

            payload = await self._fetch_listing(path)
            # The listing endpoint returns a single Listing object.
            listing_data: dict = payload["data"]  # type: ignore[index]
            children: list[dict] = listing_data.get("children", [])

            if not children:
                if not results:
                    # First page is empty — possible soft-block (playbook §4.4).
                    log.warning(
                        "r/%s returned an empty first page (dist=%s). "
                        "This may be a soft-block by Reddit on datacenter IPs, "
                        "or the subreddit is genuinely empty. "
                        "Re-run with a residential proxy if the sub is known-active.",
                        subreddit,
                        listing_data.get("dist", 0),
                    )
                break

            for child in children:
                if child.get("kind") != "t3":
                    continue
                if len(results) >= limit:
                    break
                article = self._parse_post(child["data"])
                results.append(
                    ScrapeResult(
                        status="success",
                        driver_used="curl_cffi",
                        article=article,
                    )
                )

            after = listing_data.get("after")  # None → end of listing
            count += len(children)

            if after is None:
                break

        return results[:limit]

    # ------------------------------------------------------------------
    # Single-URL scrape (engine routing entry point)
    # ------------------------------------------------------------------

    async def scrape(self, url: str) -> ScrapeResult:
        """Scrape a single Reddit post URL.

        NOTE: this single-post path is NOT yet OAuth-routed — it still uses the legacy anonymous
        ``.json`` endpoint (now 403-blocked). The daily ingest uses :meth:`scrape_subreddit`
        (which IS OAuth-routed); single-post OAuth is a follow-up if the engine routes URLs here.

        The ``.json`` endpoint for a post returns a **2-element array**:
        ``[post_listing, comments_listing]`` (playbook §1.2, §2.4).

        If ``.json`` is not already in the URL it is appended.

        Parameters
        ----------
        url:
            Full Reddit post URL
            (e.g. ``https://www.reddit.com/r/Python/comments/xyz/title/``).

        Returns
        -------
        ScrapeResult
            ``status='success'`` with the parsed post ``Article``.
        """
        # Append .json suffix if not already present.
        json_url = url if url.rstrip("/").endswith(".json") else url.rstrip("/") + ".json"

        payload = await self._fetch_json(json_url)
        # 2-element array: [post_listing, comments_listing]
        post_data: dict = payload[0]["data"]["children"][0]["data"]  # type: ignore[index]
        article = self._parse_post(post_data)

        return ScrapeResult(
            status="success",
            driver_used="curl_cffi",
            article=article,
        )

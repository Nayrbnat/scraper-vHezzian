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

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from scrapeforge.core.models import Article, ScrapeResult
from scrapeforge.core.registry import register_scraper
from scrapeforge.scrapers.community._base import CommunityScraper

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


# ---------------------------------------------------------------------------
# RedditScraper
# ---------------------------------------------------------------------------

_BASE_URL = "https://www.reddit.com"


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
            url = f"{_BASE_URL}/r/{subreddit}/{sort}.json?limit={page_size}&count={count}"
            if after:
                url += f"&after={after}"

            payload = await self._fetch_json(url)
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

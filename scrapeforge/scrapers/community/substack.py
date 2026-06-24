"""SubstackScraper — Bucket 2 community scraper for Substack publications.

Site Playbook: docs/research/substack.md.

Implementation notes
--------------------
- Uses the public Substack JSON API (no auth for public posts).
  Archive: ``/api/v1/archive?sort=new&offset=N&limit=12``
  Post body: ``/api/v1/posts/<slug>``
- Driven by ``curl_cffi`` with Chrome impersonation; follows 301 redirects for
  custom-domain publications (e.g. www.noahpinion.blog).
- Pagination is offset-based (not cursor-based); offset increments by the
  number of items RETURNED by each page.
- Paywall detection uses ``audience == "only_paid"`` (primary) and
  cleaned-text length < 500 (secondary heuristic). ``truncated_body_text``
  is NOT a paywall signal — live data confirms it appears on public posts.
  (See playbook §3 correction.)
- Phase-2 escalation to ``patchright`` (for paid content with injected auth)
  is deferred.

Per-module settings fragment (Invariant #16 — do NOT add to core Settings)
--------------------------------------------------------------------------
``SubstackSettings`` is a standalone ``pydantic_settings.BaseSettings`` fragment.
Core ``Settings`` is not touched.
"""

from __future__ import annotations

import logging
import urllib.parse
from datetime import datetime

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from selectolax.parser import HTMLParser

from scrapeforge.core.models import Article, ScrapeResult
from scrapeforge.core.registry import register_scraper
from scrapeforge.scrapers.community._base import CommunityScraper

log = logging.getLogger(__name__)

# Minimum cleaned-text length below which a post body is considered paywalled.
_MIN_CONTENT_LENGTH = 500


# ---------------------------------------------------------------------------
# Per-module settings fragment (Invariant #16)
# ---------------------------------------------------------------------------


class SubstackSettings(BaseSettings):
    """Per-module configuration for SubstackScraper.

    All values can be overridden via environment variables or a ``.env`` file.
    Never appended to core ``Settings`` (SPEC.md Invariant #16).
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    SUBSTACK_USE_CURL_CFFI: bool = Field(default=True)
    SUBSTACK_ARCHIVE_PAGE_SIZE: int = Field(default=12)
    SUBSTACK_PUBLIC_ONLY: bool = Field(default=True)
    # Comma-separated custom-domain Substacks to bind to this scraper at import
    # time (engine routes single-URL scrapes by hostname; bare-subdomain pubs
    # match *.substack.com, but custom domains like www.noahpinion.blog must be
    # registered explicitly — playbook §1, §5).
    SUBSTACK_CUSTOM_DOMAINS: str = Field(default="")

    def custom_domains(self) -> list[str]:
        """Parse ``SUBSTACK_CUSTOM_DOMAINS`` (CSV) into a clean list of hostnames."""
        return [d.strip() for d in self.SUBSTACK_CUSTOM_DOMAINS.split(",") if d.strip()]


# ---------------------------------------------------------------------------
# SubstackScraper
# ---------------------------------------------------------------------------


@register_scraper("substack.com", "www.substack.com")
class SubstackScraper(CommunityScraper):
    """Scraper for Substack publications using the public JSON API.

    Public interface
    ----------------
    - ``scrape(url)``              — single Substack post URL → one ``ScrapeResult``.
    - ``scrape_publication(pub)``  — offset-paginated archive → list of ``ScrapeResult``.

    ``<pub>`` may be a bare subdomain (``"mypub"`` → ``mypub.substack.com``),
    a full domain (``"www.noahpinion.blog"``), or a full URL.

    Custom-domain publications 301-redirect to the custom domain; curl_cffi
    follows redirects automatically so bare subdomains work too.

    The engine's ``RateLimiter`` handles inter-request politeness; this class
    does NOT sleep between requests.
    """

    BUCKET: str = "community"
    DEFAULT_DRIVER: str = "curl_cffi"

    # ------------------------------------------------------------------
    # URL normalisation
    # ------------------------------------------------------------------

    def _normalize_base(self, pub: str) -> str:
        """Normalise *pub* to a base URL (no trailing slash).

        Rules
        -----
        - Bare subdomain (no dot, no scheme) → ``https://{pub}.substack.com``
        - Full domain or URL (has a dot or scheme) → ``https://{host}``

        Examples
        --------
        ``"mypub"``                    → ``"https://mypub.substack.com"``
        ``"garymarcus.substack.com"``  → ``"https://garymarcus.substack.com"``
        ``"www.noahpinion.blog"``      → ``"https://www.noahpinion.blog"``
        ``"https://astral.substack.com/"`` → ``"https://astral.substack.com"``
        """
        # Strip trailing slash before any processing.
        pub = pub.rstrip("/")

        # Already has a scheme → parse and reconstruct cleanly.
        if pub.startswith(("http://", "https://")):
            parsed = urllib.parse.urlsplit(pub)
            return f"https://{parsed.netloc}"

        # Has a dot → treat as a domain (no scheme).
        if "." in pub:
            return f"https://{pub}"

        # No dot → bare subdomain → append .substack.com.
        return f"https://{pub}.substack.com"

    # ------------------------------------------------------------------
    # Paywall detection
    # ------------------------------------------------------------------

    def _is_paywalled(self, post: dict) -> bool:
        """Return True if *post* is behind a paywall.

        Detection rules (playbook §3)
        -----------------------------
        Primary:   ``audience == "only_paid"``
        Secondary: cleaned ``body_html`` text length < ``_MIN_CONTENT_LENGTH``

        NOTE: ``truncated_body_text`` is NOT used — live data shows it is present
        even on public posts (playbook §3 correction).
        """
        if post.get("audience") == "only_paid":
            return True

        body_html: str | None = post.get("body_html")
        if not body_html:
            return True

        cleaned = _clean_html(body_html)
        return len(cleaned) < _MIN_CONTENT_LENGTH

    # ------------------------------------------------------------------
    # Field mapping
    # ------------------------------------------------------------------

    def _build_article(self, post: dict) -> Article:
        """Map a Substack single-post object to an ``Article``.

        Field mapping (playbook §2)
        ---------------------------
        - ``url``:          ``canonical_url``
        - ``title``:        ``title``
        - ``content``:      cleaned text of ``body_html``
        - ``author``:       ``publishedBylines[0].name`` (or None)
        - ``publish_date``: ``post_date`` ISO-8601 UTC → tz-aware datetime
        - ``raw_html``:     ``body_html`` (carried for claim-check pipeline)
        - ``metadata``:     source_domain, bucket, substack_id, audience, subtitle, post_type
        """
        body_html: str | None = post.get("body_html")
        content = _clean_html(body_html) if body_html else ""

        bylines: list[dict] = post.get("publishedBylines") or []
        author: str | None = bylines[0].get("name") if bylines else None

        publish_date: datetime | None = None
        raw_post_date = post.get("post_date")
        if raw_post_date:
            try:
                publish_date = datetime.fromisoformat(raw_post_date.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                log.warning("SubstackScraper: could not parse post_date %r", raw_post_date)

        canonical_url: str = post.get("canonical_url", "")
        source_domain = urllib.parse.urlsplit(canonical_url).hostname or ""

        metadata: dict = {
            "source_domain": source_domain,
            "bucket": "community",
            "substack_id": post.get("id"),
            "audience": post.get("audience"),
            "subtitle": post.get("subtitle"),
            "post_type": post.get("type"),
        }

        return Article(
            url=canonical_url,
            title=post.get("title") or "",
            content=content,
            author=author,
            publish_date=publish_date,
            raw_html=body_html,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Paginated publication scrape
    # ------------------------------------------------------------------

    async def scrape_publication(
        self,
        pub: str,
        limit: int = 50,
        sort: str = "new",
    ) -> list[ScrapeResult]:
        """Scrape up to *limit* public posts from a Substack publication.

        Pagination follows playbook §5:
        - Fetch archive pages via ``/api/v1/archive?sort=<sort>&offset=N&limit=12``.
        - Skip ``audience != "everyone"`` items (when SUBSTACK_PUBLIC_ONLY).
        - For public items, fetch ``/api/v1/posts/<slug>`` for the full body.
        - Stop when: archive array is empty, page has fewer items than requested
          (last page), or *limit* is reached.
        - Offset increments by the number of items RETURNED (not page size).

        Parameters
        ----------
        pub:
            Publication identifier: bare subdomain, full domain, or full URL.
        limit:
            Maximum number of successful articles to return.
        sort:
            Archive sort order (``'new'`` or ``'top'``).

        Returns
        -------
        list[ScrapeResult]
            One ``ScrapeResult(status='success', article=...)`` per public, non-
            paywalled post. Truncated to *limit* items.
        """
        settings = SubstackSettings()
        page_size = settings.SUBSTACK_ARCHIVE_PAGE_SIZE
        public_only = settings.SUBSTACK_PUBLIC_ONLY

        base = self._normalize_base(pub)
        results: list[ScrapeResult] = []
        offset = 0

        while len(results) < limit:
            archive_url = (
                f"{base}/api/v1/archive?sort={sort}&search=&offset={offset}&limit={page_size}"
            )

            archive_payload = await self._fetch_json(archive_url)

            # The archive endpoint contractually returns a JSON array (playbook
            # §1). An error envelope or unexpected shape (dict/None) is treated
            # as end-of-pagination rather than crashing the loop (playbook §6).
            if not isinstance(archive_payload, list):
                log.warning(
                    "SubstackScraper: archive returned non-list payload (%s) for %s; stopping",
                    type(archive_payload).__name__,
                    base,
                )
                break

            archive_items: list = archive_payload
            if not archive_items:
                break  # Empty page → end of archive.

            for item in archive_items:
                if len(results) >= limit:
                    break

                # Pre-filter: skip paid posts without fetching their body.
                if public_only and item.get("audience") != "everyone":
                    log.debug("SubstackScraper: skipping paid post slug=%r", item.get("slug"))
                    continue

                slug: str = item.get("slug", "")
                post_url = f"{base}/api/v1/posts/{slug}"

                post = await self._fetch_json(post_url)
                if not isinstance(post, dict):
                    log.debug(
                        "SubstackScraper: post slug=%r returned non-dict payload (%s), skipping",
                        slug,
                        type(post).__name__,
                    )
                    continue

                if self._is_paywalled(post):
                    log.debug(
                        "SubstackScraper: post slug=%r is paywalled after body fetch, skipping",
                        slug,
                    )
                    continue

                article = self._build_article(post)
                results.append(
                    ScrapeResult(
                        status="success",
                        driver_used="curl_cffi",
                        article=article,
                    )
                )

            # Offset advances by items RETURNED, not page_size.
            items_returned = len(archive_items)
            offset += items_returned

            # Short page → last page of the archive.
            if items_returned < page_size:
                break

        return results[:limit]

    # ------------------------------------------------------------------
    # RSS publication scrape (avoids the rate-limited /api/v1 endpoints)
    # ------------------------------------------------------------------

    async def _fetch_raw(self, url: str) -> str:
        """Fetch *url* via the bridge and return the raw response body as a string.

        The raw-string twin of ``_fetch_json`` — used for the RSS feed (XML, not JSON). Same
        one-shot bridge lifecycle (Invariant #7): reuse an injected bridge, else create a per-call one.
        """
        bridge = self.bridge if self.bridge is not None else self._create_default_bridge(self.proxy)
        async with bridge as b:
            await b.navigate(url)
            return await b.get_html()

    async def scrape_publication_via_rss(self, pub: str, *, limit: int = 25) -> list[ScrapeResult]:
        """Scrape up to *limit* public posts from a publication's RSS feed (``<base>/feed``).

        One request per publication (the feed lists ~20 recent posts with their bodies inline),
        so it never touches the rate-limited archive/post JSON endpoints. Items whose feed body
        is truncated below ``_MIN_CONTENT_LENGTH`` are skipped (too little text to summarize).
        """
        from scrapeforge.scrapers.community.substack_rss import parse_substack_feed

        base = self._normalize_base(pub)
        xml = await self._fetch_raw(f"{base}/feed")
        return parse_substack_feed(xml, limit=limit, min_chars=_MIN_CONTENT_LENGTH)

    # ------------------------------------------------------------------
    # Single-URL scrape (engine routing entry point)
    # ------------------------------------------------------------------

    async def scrape(self, url: str) -> ScrapeResult:
        """Scrape a single Substack post URL.

        Derives the publication host and slug from the ``/p/<slug>`` URL path,
        then fetches ``/api/v1/posts/<slug>`` for the full post object.

        Parameters
        ----------
        url:
            Full Substack post URL (e.g. ``https://mypub.substack.com/p/my-post``).

        Returns
        -------
        ScrapeResult
            ``status='success'`` with the parsed ``Article``, or
            ``status='error'``, ``error='paywalled'`` if the post is behind
            a paywall (Invariant #15).
        """
        # Normalise the host the same way scrape_publication does (forces https,
        # drops any trailing slash) so both entry points derive hosts identically.
        host = self._normalize_base(url)

        # Extract slug from path like /p/<slug> or /p/<slug>/...
        parsed = urllib.parse.urlsplit(url)
        path_parts = parsed.path.strip("/").split("/")
        # Expected shape: ["p", "<slug>", ...]
        if len(path_parts) >= 2 and path_parts[0] == "p":
            slug = path_parts[1]
        else:
            # Fallback: use the last non-empty path segment.
            slug = path_parts[-1] if path_parts else ""

        post_url = f"{host}/api/v1/posts/{slug}"
        post = await self._fetch_json(post_url)
        if not isinstance(post, dict):
            return ScrapeResult(
                status="error",
                driver_used="curl_cffi",
                error="unexpected payload shape from Substack post endpoint",
            )

        if self._is_paywalled(post):
            return ScrapeResult(
                status="error",
                driver_used="curl_cffi",
                error="paywalled",
            )

        article = self._build_article(post)
        return ScrapeResult(
            status="success",
            driver_used="curl_cffi",
            article=article,
        )


# ---------------------------------------------------------------------------
# Custom-domain registration (Invariant #16 — additive, no central edits)
# ---------------------------------------------------------------------------
# The decorator above binds *.substack.com. Custom-domain publications
# (e.g. www.noahpinion.blog) 301-redirect from their bare subdomain but the
# engine routes single-URL scrapes by hostname, so each must be registered
# explicitly. Set SUBSTACK_CUSTOM_DOMAINS (CSV) in .env to bind them here at
# import time — re-binding the same class is idempotent (registry.py).
for _custom_domain in SubstackSettings().custom_domains():
    register_scraper(_custom_domain)(SubstackScraper)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _clean_html(html: str) -> str:
    """Strip HTML tags and return plain text (whitespace-normalised).

    Uses selectolax for fast, lightweight parsing (no external network calls).
    Collapses whitespace so length checks are consistent.
    """
    tree = HTMLParser(html)
    text = tree.text(deep=True, separator=" ", strip=True)
    # Collapse multiple spaces/newlines to a single space.
    return " ".join(text.split())

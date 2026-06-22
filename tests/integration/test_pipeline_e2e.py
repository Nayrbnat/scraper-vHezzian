"""Phase 6 capstone — end-to-end ingestion-pipeline proof (hermetic, @db).

Proves the WHOLE event-driven pipeline over one ephemeral Postgres + the in-memory queue
and object-store fakes, with the network fetch mocked (a fake engine returns canned raw HTML):

    POST /jobs (API: persist queued Job + publish to JOB queue)
      -> scraper worker (drain JOB queue): archive RAW to object store + publish a pointer
      -> transform worker (drain RESULTS queue): read raw -> validate/extract/normalize
         -> idempotent UPSERT into Postgres + mark Job done
      -> GET /articles (API): the stored row is served (401 without the API key)

Then re-runs the same job and asserts NO duplicate row (idempotency by PK = sha256(url)).

Marked ``@db`` (runs in CI against the pgvector service container); fully hermetic — no
network, no real Redis/MinIO. This is the Definition-of-Done proof for Phase 6.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from scrapeforge.api.app import create_app
from scrapeforge.config.settings import Settings
from scrapeforge.core.db.session import make_sessionmaker
from scrapeforge.core.models import Article, ScrapeResult
from scrapeforge.core.objectstore.memory import InMemoryObjectStore
from scrapeforge.core.queue.memory import InMemoryMessageQueue
from scrapeforge.core.storage.base import url_id
from scrapeforge.worker.scraper_worker import run_scraper_worker
from scrapeforge.worker.transform_worker import run_transform_worker

_API_KEY = "e2e-test-key"
_URL = "https://example.com/news/the-article"

# Real-looking article HTML: an <h1> title + a <div class="entry-content"> body long enough
# to clear the response_is_valid() 500-char content floor and PublicScraper's generic selectors.
_BODY = (
    "Markets moved sharply today as investors weighed the latest macroeconomic data. "
    "Analysts pointed to shifting expectations around interest rates and inflation, "
    "noting that the breadth of the move suggested broad repositioning rather than a "
    "single-sector story. Volatility ticked higher into the close, and several desks "
    "flagged elevated options activity. Commentators cautioned against reading too much "
    "into a single session, but the tone of trading reflected genuine uncertainty about "
    "the path ahead. Liquidity remained adequate, and spreads stayed orderly throughout. "
)
_ARTICLE_HTML = (
    "<html><head><title>The Article</title></head><body>"
    "<h1>The Headline That Matters</h1>"
    f'<div class="entry-content">{_BODY * 2}</div>'
    "</body></html>"
)


class _FakeEngine:
    """Stand-in for ScrapeEngine — returns a successful result with canned raw HTML,
    so the scraper worker never touches the network in this test."""

    def __init__(self, html: str) -> None:
        self._html = html

    async def scrape(self, url: str) -> ScrapeResult:
        article = Article(
            url=url,
            title="(scraper minimal-parse title — ignored by transform)",
            content="(ignored — transform re-extracts from raw_html)",
            raw_html=self._html,
            metadata={"bucket": "public", "source_domain": urlsplit(url).hostname or ""},
        )
        return ScrapeResult(status="success", driver_used="curl_cffi", article=article)


def _settings() -> Settings:
    return Settings(
        STATE_STORE_KEY="x" * 40,
        API_KEYS=_API_KEY,
        JOB_QUEUE="e2e:jobs",
        RESULTS_QUEUE="e2e:results",
        QUEUE_MAX_RETRIES=3,
    )


@pytest.mark.db
async def test_pipeline_end_to_end(db_session: AsyncSession, _db_url: str) -> None:
    settings = _settings()
    session_factory = make_sessionmaker(db_session.bind)
    queue = InMemoryMessageQueue()
    store = InMemoryObjectStore()
    app = create_app(settings=settings, session_factory=session_factory, queue=queue)
    auth = {"X-API-Key": _API_KEY}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # --- 1. Enqueue a scrape job via the API (persists a queued Job + publishes) ---
        r = await client.post("/jobs", json={"source": _URL}, headers=auth)
        assert r.status_code == 202, r.text
        job_id = r.json()["id"]
        assert await queue.size(settings.JOB_QUEUE) == 1  # one job message on the bus
        assert r.json()["status"] == "queued"

        # --- 2. Scraper worker drains the JOB queue: archive raw + publish a pointer ---
        await run_scraper_worker(
            queue=queue, store=store, engine=_FakeEngine(_ARTICLE_HTML), settings=settings
        )
        assert await queue.size(settings.RESULTS_QUEUE) == 1  # one pointer produced
        # raw payload archived under the deterministic claim-check key
        assert await store.exists(f"raw/public/{url_id(_URL)}")

        # --- 3. Transform worker drains RESULTS: read raw -> normalize -> UPSERT -> Job done ---
        await run_transform_worker(
            queue=queue, store=store, session_factory=session_factory, settings=settings
        )

        # --- 4. Serving API returns the stored article ---
        r = await client.get("/articles", headers=auth)
        assert r.status_code == 200
        articles = r.json()
        assert len(articles) == 1
        art = articles[0]
        assert art["url"] == _URL
        assert art["id"] == url_id(_URL)
        assert art["domain"] == "example.com"
        assert "Headline That Matters" in art["title"]
        assert len(art["content"]) >= 500  # transform re-extracted the body from raw
        assert art["raw_key"] == f"raw/public/{url_id(_URL)}"  # claim-check pointer persisted

        # GET by id works
        r_one = await client.get(f"/articles/{art['id']}", headers=auth)
        assert r_one.status_code == 200

        # --- 5. Auth: serving endpoints require the API key (Invariant #18 / fail-closed) ---
        assert (await client.get("/articles")).status_code == 401

        # --- 6. The job is marked done with result_count == 1 ---
        r_job = await client.get(f"/jobs/{job_id}", headers=auth)
        assert r_job.status_code == 200
        assert r_job.json()["status"] == "done"
        assert r_job.json()["result_count"] == 1

        # --- 7. Idempotency: re-run the SAME url end-to-end -> still exactly ONE row ---
        r2 = await client.post("/jobs", json={"source": _URL}, headers=auth)
        assert r2.status_code == 202
        await run_scraper_worker(
            queue=queue, store=store, engine=_FakeEngine(_ARTICLE_HTML), settings=settings
        )
        await run_transform_worker(
            queue=queue, store=store, session_factory=session_factory, settings=settings
        )
        r = await client.get("/articles", headers=auth)
        assert len(r.json()) == 1  # UPSERT by PK = sha256(url): no duplicate

# Community Ingestion (Phase 1, lean) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Daily, the 50 curated Substacks scrape their new articles into Postgres (deduped, queryable, one row per article) by reusing the existing `SubstackScraper.scrape_publication` + `PostgresSink` — no discovery/transform/envelope surgery.

**Architecture:** The scheduler routes community-publication `Source` rows (those with `params.platform`) to a new `INGEST` queue. A new `community_ingest_worker` consumes each message, runs the bucket scraper's `scrape_publication` (already returns fully-parsed `Article`s), archives each new post's raw payload to the object store (claim-check), and persists structured rows via the existing idempotent `PostgresSink`. It owns the Job lifecycle for community sources. The HTML scraper→transform path is untouched.

**Tech Stack:** Python 3.12 async, SQLAlchemy 2.0 async + asyncpg + pgvector, Typer CLI, Redis/arq (behind the `MessageQueue` port), MinIO/S3 (behind the `ObjectStore` port), pytest (`asyncio_mode=auto`), ruff. `@db` tests use an ephemeral pgvector container.

**Reference spec:** `docs/superpowers/specs/2026-06-23-community-ingestion-design.md`

---

## Conventions for every task

- **TDD:** write the failing test, run it red, implement minimally, run it green, commit.
- **Gate before each commit:** `.venv/Scripts/python.exe -m ruff format <files>` then
  `.venv/Scripts/python.exe -m ruff check <files>` (0 errors).
- **`@db` tests** are skipped unless `DATABASE_URL` points at a reachable pgvector. To run them
  locally, start a container once:
  ```bash
  docker run -d --rm --name sf-pg -e POSTGRES_USER=scrapeforge -e POSTGRES_PASSWORD=scrapeforge \
    -e POSTGRES_DB=scrapeforge -p 5439:5432 pgvector/pgvector:pg16
  export DATABASE_URL="postgresql+asyncpg://scrapeforge:scrapeforge@localhost:5439/scrapeforge"
  ```
- **Commit message footer (every commit):**
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Never** edit `engine.py`, `core/registry.py`, `core/db/repositories.py`, the root `cli.py`,
  or the existing `scraper_worker.py` / `transform_worker.py`.

## File map (what each task creates/touches)

| Task | Files |
|---|---|
| 1 | `scrapeforge/worker/messages.py` (+`IngestMessage`), `scrapeforge/config/settings.py` (+`INGEST_QUEUE`), `tests/test_messages.py` (new), `tests/test_settings.py` (+1) |
| 2 | `scrapeforge/scrapers/community/substack_sources.py` (+`seed_sources`), `scrapeforge/scrapers/community/cli.py` (+`seed-substacks`), `tests/test_substack_sources.py` (+tests) |
| 3 | `scrapeforge/worker/scheduler.py` (routing), `tests/test_scheduler.py` (+tests) |
| 4 | `scrapeforge/worker/community_ingest_worker.py` (new), `tests/test_community_ingest_worker.py` (new) |
| 5 | `scrapeforge/worker/run_community_ingest.py` (new), `deployment/docker-compose.yml` (+service), `tests/test_run_community_ingest.py` (new, smoke) |
| 6 | `tests/test_community_ingest_e2e.py` (new) |
| 7 | `SPEC.md`, `architecture.MD`, `planning.MD`, the `community-publication-fanout-gap` memory (no tests) |

---

## Task 1: `IngestMessage` contract + `INGEST_QUEUE` setting

**Files:**
- Modify: `scrapeforge/worker/messages.py`
- Modify: `scrapeforge/config/settings.py:94-97`
- Test: `tests/test_messages.py` (create), `tests/test_settings.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_messages.py`:
```python
"""Contract tests for worker message TypedDicts."""

from __future__ import annotations


def test_ingest_message_has_expected_keys() -> None:
    from scrapeforge.worker.messages import IngestMessage

    assert set(IngestMessage.__annotations__) == {
        "job_id",
        "platform",
        "target",
        "bucket",
        "limit",
    }
```

Append to `tests/test_settings.py` (inside the module, top-level function):
```python
def test_ingest_queue_default(fake_env) -> None:
    from scrapeforge.config.settings import Settings

    assert Settings().INGEST_QUEUE == "scrapeforge:ingest"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_messages.py tests/test_settings.py::test_ingest_queue_default -q`
Expected: FAIL (`ImportError: cannot import name 'IngestMessage'` / `AttributeError: INGEST_QUEUE`).

- [ ] **Step 3: Implement**

In `scrapeforge/worker/messages.py`, add after the `JobMessage` class:
```python
class IngestMessage(TypedDict):
    """INGEST-queue payload: scrape a whole publication (scheduler → community-ingest worker).

    Unlike ``JobMessage`` (one URL → one raw object), this names a *publication* that the
    community-ingest worker scrapes via the bucket scraper's ``scrape_publication``.
    """

    job_id: str
    platform: str  # e.g. "substack"
    target: str  # publication host, e.g. "newsletter.semianalysis.com"
    bucket: str  # "community"
    limit: int  # max posts to fetch this run
```

In `scrapeforge/config/settings.py`, add directly below the `RESULTS_QUEUE` line (currently line 95):
```python
    INGEST_QUEUE: str = "scrapeforge:ingest"  # scheduler -> community-ingest workers (publications)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_messages.py tests/test_settings.py::test_ingest_queue_default -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Gate + commit**

```bash
.venv/Scripts/python.exe -m ruff format scrapeforge/worker/messages.py scrapeforge/config/settings.py tests/test_messages.py tests/test_settings.py
.venv/Scripts/python.exe -m ruff check scrapeforge/worker/messages.py scrapeforge/config/settings.py tests/test_messages.py tests/test_settings.py
git add scrapeforge/worker/messages.py scrapeforge/config/settings.py tests/test_messages.py tests/test_settings.py
git commit -m "feat(worker): add IngestMessage contract + INGEST_QUEUE setting

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Restore `seed_sources` (atomic upsert) + `seed-substacks` CLI

**Files:**
- Modify: `scrapeforge/scrapers/community/substack_sources.py` (add `seed_sources` after the selection helpers)
- Modify: `scrapeforge/scrapers/community/cli.py` (add `seed-substacks` command)
- Test: `tests/test_substack_sources.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_substack_sources.py`:
```python
import pytest


@pytest.mark.db
class TestSeedSources:
    async def test_seeds_all_sources(self, db_session) -> None:
        from sqlalchemy import select

        from scrapeforge.core.db.models import Source
        from scrapeforge.scrapers.community.substack_sources import (
            SUBSTACK_INVESTING_SOURCES,
            seed_sources,
        )

        n = await seed_sources(db_session, limit=5)
        assert n == len(SUBSTACK_INVESTING_SOURCES)

        rows = (await db_session.execute(select(Source))).scalars().all()
        assert len(rows) == len(SUBSTACK_INVESTING_SOURCES)
        assert all(r.bucket == "community" for r in rows)
        assert all(r.params["platform"] == "substack" for r in rows)
        assert all(r.params["limit"] == 5 for r in rows)

    async def test_seeding_is_idempotent(self, db_session) -> None:
        from sqlalchemy import func, select

        from scrapeforge.core.db.models import Source
        from scrapeforge.scrapers.community.substack_sources import (
            SUBSTACK_INVESTING_SOURCES,
            seed_sources,
        )

        await seed_sources(db_session, limit=5)
        await seed_sources(db_session, limit=5)  # second run must not duplicate

        total = await db_session.scalar(select(func.count()).select_from(Source))
        assert total == len(SUBSTACK_INVESTING_SOURCES)

    async def test_reseeding_updates_params(self, db_session) -> None:
        from sqlalchemy import select

        from scrapeforge.core.db.models import Source
        from scrapeforge.scrapers.community.substack_sources import seed_sources

        await seed_sources(db_session, limit=5)
        await seed_sources(db_session, limit=42)  # change the per-source limit

        row = (await db_session.execute(select(Source).limit(1))).scalars().first()
        assert row is not None
        assert row.params["limit"] == 42


class TestSeedSubstacksCli:
    def test_dry_run_lists_without_writing(self) -> None:
        from typer.testing import CliRunner

        from scrapeforge.cli import app

        result = CliRunner().invoke(app, ["community", "seed-substacks", "--dry-run"])
        assert result.exit_code == 0
        assert "50 curated sources" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_substack_sources.py::TestSeedSubstacksCli -q`
Expected: FAIL (`No such command 'seed-substacks'` → non-zero exit).

(The `@db` `TestSeedSources` fails on `ImportError: cannot import name 'seed_sources'`; it is skipped unless `DATABASE_URL` is set — run it explicitly with the container in Step 4.)

- [ ] **Step 3: Implement `seed_sources`**

In `scrapeforge/scrapers/community/substack_sources.py`, add at the end of the file (after `select_sources`):
```python
async def seed_sources(session, *, limit: int = 25, enabled: bool = True) -> int:
    """Idempotently upsert the curated publications into the ``sources`` table.

    Uses a single atomic ``INSERT ... ON CONFLICT (name) DO UPDATE`` so re-running never
    duplicates and a concurrent re-run cannot raise on the unique ``Source.name``.  Each
    row is a community publication source the scheduler routes to the INGEST queue:
    ``params = {"url": <host>, "platform": "substack", "limit": <limit>}``.

    The query is inlined here rather than added to ``repositories.py`` — that file is
    off-limits for feature additions (Invariant #17); the scheduler inlines its own
    ``Source`` query for the same reason.

    Args:
        session: Open ``AsyncSession`` (committed before returning).
        limit:   Per-source post cap stored in ``params['limit']``.
        enabled: Whether seeded sources are scheduler-enabled.

    Returns:
        Number of curated sources processed — always ``len(SUBSTACK_INVESTING_SOURCES)``.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from scrapeforge.core.db.models import Source

    rows = [
        {
            "name": f"substack:{s.base}",
            "bucket": "community",
            "params": {"url": s.base, "platform": "substack", "limit": limit},
            "cron": None,
            "enabled": enabled,
        }
        for s in SUBSTACK_INVESTING_SOURCES
    ]
    stmt = pg_insert(Source).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["name"],
        set_={
            "bucket": stmt.excluded.bucket,
            "params": stmt.excluded.params,
            "cron": stmt.excluded.cron,
            "enabled": stmt.excluded.enabled,
        },
    )
    await session.execute(stmt)
    await session.commit()
    return len(rows)
```

- [ ] **Step 4: Implement the CLI command**

In `scrapeforge/scrapers/community/cli.py`, add a new command at the end of the file:
```python
@community_app.command("seed-substacks")
def seed_substacks(
    limit: int = typer.Option(
        25, "--limit", "-l", help="Per-source post cap stored on each Source"
    ),
    database_url: str | None = typer.Option(
        None, "--database-url", help="Override DATABASE_URL (defaults to Settings)"
    ),
    enabled: bool = typer.Option(
        True, "--enabled/--disabled", help="Seed sources as scheduler-enabled or not"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the curated list without touching the database"
    ),
) -> None:
    """Seed the curated investing-Substack list into the ``sources`` table (idempotent).

    The scheduler then routes these community publications to the INGEST queue, where the
    community-ingest worker scrapes each on its daily tick.
    """
    from scrapeforge.scrapers.community.substack_sources import (
        SUBSTACK_INVESTING_SOURCES,
        seed_sources,
    )

    if dry_run:
        for s in SUBSTACK_INVESTING_SOURCES:
            flag = " [paid-leaning]" if s.paywall else ""
            typer.echo(f"{s.sector:22s} {s.name:30s} {s.url}{flag}")
        typer.echo(
            f"\n{len(SUBSTACK_INVESTING_SOURCES)} curated sources (dry-run, nothing written)."
        )
        return

    _use_selector_loop()

    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    async def _run() -> int:
        engine = make_engine(database_url)
        session_factory = make_sessionmaker(engine)
        try:
            async with session_factory() as session:
                return await seed_sources(session, limit=limit, enabled=enabled)
        finally:
            await engine.dispose()

    count = asyncio.run(_run())
    state = "enabled" if enabled else "disabled"
    typer.echo(f"Seeded {count} Substack sources ({state}, limit={limit}) into the sources table.")
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
.venv/Scripts/python.exe -m pytest tests/test_substack_sources.py::TestSeedSubstacksCli -q
# With the pgvector container + DATABASE_URL exported:
.venv/Scripts/python.exe -m pytest tests/test_substack_sources.py::TestSeedSources -q -m db
```
Expected: PASS (CLI test; 3 `@db` tests pass against the container).

- [ ] **Step 6: Gate + commit**

```bash
.venv/Scripts/python.exe -m ruff format scrapeforge/scrapers/community/substack_sources.py scrapeforge/scrapers/community/cli.py tests/test_substack_sources.py
.venv/Scripts/python.exe -m ruff check scrapeforge/scrapers/community/substack_sources.py scrapeforge/scrapers/community/cli.py tests/test_substack_sources.py
git add scrapeforge/scrapers/community/substack_sources.py scrapeforge/scrapers/community/cli.py tests/test_substack_sources.py
git commit -m "feat(community): seed curated Substacks as community Sources (atomic upsert) + CLI

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Scheduler routes publication sources to the INGEST queue

**Files:**
- Modify: `scrapeforge/worker/scheduler.py:84-104` (the per-source loop body)
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_scheduler.py` (it already has `@db` fixtures + an `InMemoryMessageQueue`; mirror the existing style). Add:
```python
@pytest.mark.db
async def test_community_platform_source_routes_to_ingest_queue(
    db_session,
    session_factory,
) -> None:
    """A Source with params.platform publishes an IngestMessage to INGEST_QUEUE."""
    import types

    from scrapeforge.core.db.models import Source
    from scrapeforge.core.queue.memory import InMemoryMessageQueue
    from scrapeforge.worker.scheduler import enqueue_due_sources

    async with session_factory() as s:
        s.add(
            Source(
                name="substack:newsletter.semianalysis.com",
                bucket="community",
                params={
                    "url": "newsletter.semianalysis.com",
                    "platform": "substack",
                    "limit": 7,
                },
                cron=None,
                enabled=True,
            )
        )
        await s.commit()

    queue = InMemoryMessageQueue()
    settings = types.SimpleNamespace(JOB_QUEUE="jobs", INGEST_QUEUE="ingest")

    n = await enqueue_due_sources(session_factory=session_factory, queue=queue, settings=settings)

    assert n == 1
    assert await queue.size("ingest") == 1
    assert await queue.size("jobs") == 0
    msg = await queue.reserve("ingest")
    assert msg is not None
    assert msg.payload["platform"] == "substack"
    assert msg.payload["target"] == "newsletter.semianalysis.com"
    assert msg.payload["bucket"] == "community"
    assert msg.payload["limit"] == 7
    assert msg.payload["job_id"]


@pytest.mark.db
async def test_non_platform_source_still_routes_to_job_queue(
    db_session,
    session_factory,
) -> None:
    """A Source without params.platform keeps today's JobMessage/JOB_QUEUE behaviour."""
    import types

    from scrapeforge.core.db.models import Source
    from scrapeforge.core.queue.memory import InMemoryMessageQueue
    from scrapeforge.worker.scheduler import enqueue_due_sources

    async with session_factory() as s:
        s.add(
            Source(
                name="ft.com-daily",
                bucket="premium",
                params={"url": "https://ft.com/x"},
                cron=None,
                enabled=True,
            )
        )
        await s.commit()

    queue = InMemoryMessageQueue()
    settings = types.SimpleNamespace(JOB_QUEUE="jobs", INGEST_QUEUE="ingest")

    await enqueue_due_sources(session_factory=session_factory, queue=queue, settings=settings)

    assert await queue.size("jobs") == 1
    assert await queue.size("ingest") == 0
```

> If `tests/test_scheduler.py` does not already define a `session_factory` fixture, add the
> same one used in `tests/test_transform_worker.py:124-133`:
> ```python
> import pytest
> from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
> from scrapeforge.core.db.session import make_sessionmaker
>
> @pytest.fixture
> def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
>     engine = create_async_engine(_db_url, echo=False)
>     return make_sessionmaker(engine)
> ```

- [ ] **Step 2: Run tests to verify they fail**

Run (with the container + `DATABASE_URL`):
`.venv/Scripts/python.exe -m pytest tests/test_scheduler.py::test_community_platform_source_routes_to_ingest_queue -q -m db`
Expected: FAIL (the message lands on `jobs`, not `ingest`, because routing doesn't exist yet).

- [ ] **Step 3: Implement the routing**

In `scrapeforge/worker/scheduler.py`, change the existing import line
`from scrapeforge.worker.messages import JobMessage` to:
```python
from scrapeforge.worker.messages import IngestMessage, JobMessage
```
Replace the body of the `for source in sources:` loop (currently lines 84-106) with:
```python
        for source in sources:
            job_id = uuid.uuid4().hex

            # Persist the Job row (mirrors POST /jobs behaviour).
            await create_job(
                session,
                job_id=job_id,
                source=source.name,
                params=source.params,
            )

            platform = source.params.get("platform")
            if platform:
                # Community *publication* source → fan-in to the ingest worker.
                ingest: IngestMessage = {
                    "job_id": job_id,
                    "platform": platform,
                    "target": source.params.get("url") or source.name,
                    "bucket": source.bucket,
                    "limit": int(source.params.get("limit", 25)),
                }
                await queue.publish(settings.INGEST_QUEUE, ingest)
            else:
                # Single-URL source → today's scraper-worker path (unchanged).
                url = source.params.get("url") or source.name
                message: JobMessage = {
                    "job_id": job_id,
                    "url": url,
                    "bucket": source.bucket,
                }
                await queue.publish(settings.JOB_QUEUE, message)

            count += 1
```

- [ ] **Step 4: Run tests to verify they pass**

Run (with container + `DATABASE_URL`):
`.venv/Scripts/python.exe -m pytest tests/test_scheduler.py -q -m db`
Expected: PASS (new + existing scheduler `@db` tests green).

- [ ] **Step 5: Gate + commit**

```bash
.venv/Scripts/python.exe -m ruff format scrapeforge/worker/scheduler.py tests/test_scheduler.py
.venv/Scripts/python.exe -m ruff check scrapeforge/worker/scheduler.py tests/test_scheduler.py
git add scrapeforge/worker/scheduler.py tests/test_scheduler.py
git commit -m "feat(worker): scheduler routes platform Sources to the INGEST queue

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `community_ingest_worker`

**Files:**
- Create: `scrapeforge/worker/community_ingest_worker.py`
- Test: `tests/test_community_ingest_worker.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_community_ingest_worker.py`:
```python
"""Tests for the community-ingest worker (Phase 1 lean).

Reuses the in-memory fakes for queue + object store; ``@db`` tests use the ephemeral
pgvector instance via the ``session_factory`` fixture (mirrors test_transform_worker).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scrapeforge.core.db.repositories import create_job
from scrapeforge.core.db.session import make_sessionmaker
from scrapeforge.core.models import Article, ScrapeResult
from scrapeforge.core.objectstore.memory import InMemoryObjectStore
from scrapeforge.core.storage.base import url_id
from scrapeforge.worker.messages import IngestMessage, raw_object_key


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(_db_url, echo=False)
    return make_sessionmaker(engine)


class _FakeScraper:
    """Returns canned scrape_publication results without network I/O."""

    def __init__(self, results: list[ScrapeResult]) -> None:
        self._results = results
        self.calls: list[tuple[str, int]] = []

    async def scrape_publication(self, target, limit=50, sort="new"):  # noqa: ARG002
        self.calls.append((target, limit))
        return list(self._results)


def _success(url: str, title: str) -> ScrapeResult:
    return ScrapeResult(
        status="success",
        driver_used="curl_cffi",
        article=Article(
            url=url,
            title=title,
            content="Body text here, long enough to be a real article body.",
            author="A. Writer",
            raw_html=f"<div>{title}</div>",
            metadata={"bucket": "community", "source_domain": "chipstrat.com"},
        ),
    )


def _paywalled() -> ScrapeResult:
    return ScrapeResult(status="error", driver_used="curl_cffi", article=None, error="paywalled")


def _msg(job_id: str) -> IngestMessage:
    return IngestMessage(
        job_id=job_id, platform="substack", target="www.chipstrat.com", bucket="community", limit=5
    )


@pytest.mark.db
async def test_persists_success_articles_and_skips_paywalled(
    db_session: AsyncSession, session_factory
) -> None:
    from scrapeforge.core.db.models import Article as ArticleRow
    from scrapeforge.core.db.models import Job as JobRow
    from scrapeforge.worker.community_ingest_worker import handle_ingest_job

    job_id = uuid.uuid4().hex
    async with session_factory() as s:
        await create_job(s, job_id=job_id, source="www.chipstrat.com", params={})

    url_a = "https://www.chipstrat.com/p/a"
    url_b = "https://www.chipstrat.com/p/b"
    scraper = _FakeScraper([_success(url_a, "Alpha"), _paywalled(), _success(url_b, "Beta")])
    store = InMemoryObjectStore()

    persisted = await handle_ingest_job(
        _msg(job_id), store=store, session_factory=session_factory, scraper=scraper
    )

    assert persisted == 2
    # Both raw payloads archived under the deterministic community key.
    assert await store.exists(raw_object_key("community", url_id(url_a)))
    assert await store.exists(raw_object_key("community", url_id(url_b)))
    # Both rows persisted WITH parsed fields (title/author) — no CSS re-extraction.
    row_a = await db_session.get(ArticleRow, url_id(url_a))
    assert row_a is not None and row_a.title == "Alpha" and row_a.author == "A. Writer"
    assert row_a.bucket == "community"
    # Paywalled post is absent.
    paywall_id = url_id("https://www.chipstrat.com/p/paywalled")
    assert await db_session.get(ArticleRow, paywall_id) is None
    # Job done with the right count.
    job = await db_session.get(JobRow, job_id)
    assert job.status == "done" and job.result_count == 2 and job.finished_at is not None


@pytest.mark.db
async def test_rerun_produces_no_duplicate_rows(db_session: AsyncSession, session_factory) -> None:
    from sqlalchemy import func, select

    from scrapeforge.core.db.models import Article as ArticleRow
    from scrapeforge.worker.community_ingest_worker import handle_ingest_job

    url_a = "https://www.chipstrat.com/p/a"
    results = [_success(url_a, "Alpha")]

    for _ in range(2):
        job_id = uuid.uuid4().hex
        async with session_factory() as s:
            await create_job(s, job_id=job_id, source="www.chipstrat.com", params={})
        await handle_ingest_job(
            _msg(job_id),
            store=InMemoryObjectStore(),
            session_factory=session_factory,
            scraper=_FakeScraper(results),
        )

    total = await db_session.scalar(select(func.count()).select_from(ArticleRow))
    assert total == 1  # idempotent UPSERT — no duplicate


@pytest.mark.db
async def test_scrape_failure_marks_job_error_and_reraises(
    db_session: AsyncSession, session_factory
) -> None:
    from scrapeforge.core.db.models import Job as JobRow
    from scrapeforge.worker.community_ingest_worker import handle_ingest_job

    class _Boom:
        async def scrape_publication(self, target, limit=50, sort="new"):
            raise RuntimeError("publication down")

    job_id = uuid.uuid4().hex
    async with session_factory() as s:
        await create_job(s, job_id=job_id, source="www.chipstrat.com", params={})

    with pytest.raises(RuntimeError, match="publication down"):
        await handle_ingest_job(
            _msg(job_id),
            store=InMemoryObjectStore(),
            session_factory=session_factory,
            scraper=_Boom(),
        )

    job = await db_session.get(JobRow, job_id)
    assert job.status == "error" and job.error and "publication down" in job.error
```

- [ ] **Step 2: Run tests to verify they fail**

Run (container + `DATABASE_URL`):
`.venv/Scripts/python.exe -m pytest tests/test_community_ingest_worker.py -q -m db`
Expected: FAIL (`ModuleNotFoundError: scrapeforge.worker.community_ingest_worker`).

- [ ] **Step 3: Implement the worker**

Create `scrapeforge/worker/community_ingest_worker.py`:
```python
"""Community-ingestion worker (Phase 1, lean) — scheduled publication scrape → Postgres.

Consumes an ``IngestMessage`` from the INGEST queue, runs the community bucket scraper's
``scrape_publication`` (which already returns fully-parsed ``Article``s), archives each new
post's raw payload to the object store (claim-check), and persists structured rows via the
existing idempotent ``PostgresSink``.  Owns the full Job lifecycle for community sources
(queued → running → done | error).

Invariant #18 carve-out: fully-parsing community/JSON scrapers persist in-worker; the
scraper→transform HTML claim-check split governs *public-bucket HTML* only.  Re-extracting
Substack's JSON-sourced fields via CSS selectors would lose title/author/date, so this worker
deliberately writes the scraper's already-parsed article straight to the sink.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scrapeforge.core.db.repositories import update_job_status
from scrapeforge.core.objectstore.base import ObjectStore
from scrapeforge.core.queue.base import MessageQueue
from scrapeforge.core.storage.base import url_id
from scrapeforge.core.storage.postgres import PostgresSink
from scrapeforge.worker.messages import IngestMessage, raw_object_key

log = logging.getLogger(__name__)


def _resolve_scraper(platform: str):
    """Lazy-resolve the community scraper for *platform* (no eager bucket imports).

    Mirrors the CLI's platform dispatch so this worker doesn't import every bucket at
    module load.  Reddit slots in later by adding one branch.
    """
    if platform == "substack":
        from scrapeforge.scrapers.community.substack import SubstackScraper

        return SubstackScraper()
    raise ValueError(f"no community-ingest scraper for platform {platform!r}")


async def handle_ingest_job(
    payload: IngestMessage,
    *,
    store: ObjectStore,
    session_factory: async_sessionmaker[AsyncSession],
    scraper=None,
) -> int:
    """Scrape one publication and persist its successful articles. Returns # persisted.

    Steps: mark Job running → run ``scrape_publication`` → for each ``success`` article:
    skip if already seen this run, archive raw (claim-check), UPSERT via ``PostgresSink`` →
    mark Job done.  A raised scrape/persist error marks the Job ``error`` and re-raises so the
    ``MessageQueue`` retries → DLQ.

    Args:
        payload:         The ``IngestMessage`` from the INGEST queue.
        store:           Object-store backend (raw archive).
        session_factory: ``async_sessionmaker`` for the serving DB.
        scraper:         Optional injected scraper (tests); resolved by platform otherwise.
    """
    job_id = payload["job_id"]
    target = payload["target"]
    bucket = payload["bucket"]
    limit = payload["limit"]

    scraper = scraper if scraper is not None else _resolve_scraper(payload["platform"])

    async with session_factory() as session:
        await update_job_status(session, job_id, status="running", started=True)

    sink = PostgresSink(session_factory)
    persisted = 0
    try:
        results = await scraper.scrape_publication(target, limit=limit)
        for result in results:
            if result.status != "success" or result.article is None:
                continue
            article = result.article
            if sink.seen(article.url):
                continue
            key = raw_object_key(bucket, url_id(article.url))
            if article.raw_html:
                raw = article.raw_html.encode("utf-8")
                content_type = "text/html; charset=utf-8"
            else:
                raw = json.dumps(
                    {"status": result.status, "url": article.url, "title": article.title}
                ).encode("utf-8")
                content_type = "application/json"
            await store.put(key, raw, content_type)
            # Carry the claim-check pointer into the persisted row's metadata.
            article.metadata.setdefault("raw_key", key)
            await sink.write(result)
            persisted += 1
    except Exception as exc:  # noqa: BLE001
        async with session_factory() as session:
            await update_job_status(
                session,
                job_id,
                status="error",
                result_count=persisted,
                error=str(exc),
                finished=True,
            )
        raise  # let the MessageQueue retry → DLQ

    async with session_factory() as session:
        await update_job_status(
            session, job_id, status="done", result_count=persisted, finished=True
        )
    log.info("community-ingest: job=%s target=%s persisted=%d", job_id, target, persisted)
    return persisted


async def run_community_ingest_worker(
    *,
    queue: MessageQueue,
    store: ObjectStore,
    session_factory: async_sessionmaker[AsyncSession],
    settings,
) -> None:
    """Drain the INGEST queue until empty (Phase-1 drain loop).

    Each message is handled by ``handle_ingest_job``; retry/DLQ is delegated to the
    ``MessageQueue`` port (``consume_once``).
    """

    async def _handler(msg: dict) -> None:
        await handle_ingest_job(
            msg,  # type: ignore[arg-type]
            store=store,
            session_factory=session_factory,
        )

    while await queue.consume_once(
        settings.INGEST_QUEUE, _handler, max_retries=settings.QUEUE_MAX_RETRIES
    ):
        pass  # keep draining until the queue is empty
```

> Import note: `raw_object_key` lives in `scrapeforge/worker/messages.py` (re-used by the
> scraper worker); `url_id` lives in `scrapeforge/core/storage/base.py`. The imports above
> reflect that split — do not redefine either helper.

- [ ] **Step 4: Run tests to verify they pass**

Run (container + `DATABASE_URL`):
`.venv/Scripts/python.exe -m pytest tests/test_community_ingest_worker.py -q -m db`
Expected: PASS (3 passed).

- [ ] **Step 5: Gate + commit**

```bash
.venv/Scripts/python.exe -m ruff format scrapeforge/worker/community_ingest_worker.py tests/test_community_ingest_worker.py
.venv/Scripts/python.exe -m ruff check scrapeforge/worker/community_ingest_worker.py tests/test_community_ingest_worker.py
git add scrapeforge/worker/community_ingest_worker.py tests/test_community_ingest_worker.py
git commit -m "feat(worker): community-ingest worker (scrape_publication -> PostgresSink)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Deployment entry point + compose service

**Files:**
- Create: `scrapeforge/worker/run_community_ingest.py`
- Modify: `deployment/docker-compose.yml`
- Test: `tests/test_run_community_ingest.py`

- [ ] **Step 1: Write the failing smoke test**

Create `tests/test_run_community_ingest.py`:
```python
"""Smoke test: the community-ingest deployment entry imports and exposes main()."""

from __future__ import annotations


def test_entry_point_exposes_async_main() -> None:
    import inspect

    from scrapeforge.worker import run_community_ingest

    assert inspect.iscoroutinefunction(run_community_ingest.main)
```

- [ ] **Step 2: Run it red**

Run: `.venv/Scripts/python.exe -m pytest tests/test_run_community_ingest.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement the entry point**

Create `scrapeforge/worker/run_community_ingest.py` (mirrors `run_transform.py`):
```python
"""Deployment entry point for the COMMUNITY-INGEST worker (Phase 1).

Wires the real adapters (RedisQueue + MinioStore + async DB session factory) and runs the
``run_community_ingest_worker`` drain loop forever, polling the INGEST queue. Persists
community-publication articles directly (Invariant #18 carve-out). Run via
``python -m scrapeforge.worker.run_community_ingest``.
"""

from __future__ import annotations

import asyncio

import redis.asyncio as aioredis

from scrapeforge.config.settings import Settings
from scrapeforge.core.db.session import make_engine, make_sessionmaker
from scrapeforge.core.objectstore.minio_store import MinioStore
from scrapeforge.core.queue.redis_queue import RedisQueue
from scrapeforge.worker.community_ingest_worker import run_community_ingest_worker

_POLL_INTERVAL_S = 2.0


async def main() -> None:
    settings = Settings()
    queue = RedisQueue(aioredis.from_url(settings.REDIS_URL), dlq_suffix=settings.DLQ_SUFFIX)
    store = MinioStore.from_settings(settings)
    session_factory = make_sessionmaker(make_engine(settings.DATABASE_URL))
    while True:  # poll: drain the INGEST queue, then idle briefly
        await run_community_ingest_worker(
            queue=queue, store=store, session_factory=session_factory, settings=settings
        )
        await asyncio.sleep(_POLL_INTERVAL_S)


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 4: Run it green**

Run: `.venv/Scripts/python.exe -m pytest tests/test_run_community_ingest.py -q`
Expected: PASS.

- [ ] **Step 5: Add the compose service**

In `deployment/docker-compose.yml`, add after the `transform-worker` service block (before the
`scheduler` service):
```yaml
  # --- Community-ingest worker: scrapes whole publications (the 50 Substacks) on the INGEST
  #     queue and persists their parsed articles directly (Invariant #18 carve-out). No browser. ---
  community-ingest:
    build:
      context: ..
      dockerfile: deployment/Dockerfile.api
    command: ["python", "-m", "scrapeforge.worker.run_community_ingest"]
    environment: *app-env
    depends_on:
      postgres: { condition: service_healthy }
      redis: { condition: service_started }
      minio: { condition: service_started }
    restart: unless-stopped
```

- [ ] **Step 6: Verify compose parses**

Run: `docker compose -f deployment/docker-compose.yml config >/dev/null && echo OK`
Expected: `OK` (no parse error). (If `docker` is unavailable in the environment, note it and rely
on CI to validate; the YAML mirrors the existing `transform-worker` block exactly.)

- [ ] **Step 7: Gate + commit**

```bash
.venv/Scripts/python.exe -m ruff format scrapeforge/worker/run_community_ingest.py tests/test_run_community_ingest.py
.venv/Scripts/python.exe -m ruff check scrapeforge/worker/run_community_ingest.py tests/test_run_community_ingest.py
git add scrapeforge/worker/run_community_ingest.py tests/test_run_community_ingest.py deployment/docker-compose.yml
git commit -m "feat(deploy): community-ingest worker entry point + compose service

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Hermetic end-to-end test

**Files:**
- Test: `tests/test_community_ingest_e2e.py`

- [ ] **Step 1: Write the end-to-end test**

Create `tests/test_community_ingest_e2e.py`:
```python
"""Hermetic end-to-end: seed → scheduler → community-ingest worker → Postgres.

Proves the whole Phase-1 chain with fakes for queue + object store and an ephemeral PG.
The scraper is monkeypatched so no network is touched.
"""

from __future__ import annotations

import types

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scrapeforge.core.db.session import make_sessionmaker
from scrapeforge.core.models import Article, ScrapeResult
from scrapeforge.core.objectstore.memory import InMemoryObjectStore
from scrapeforge.core.queue.memory import InMemoryMessageQueue


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(_db_url, echo=False)
    return make_sessionmaker(engine)


class _FakeScraper:
    def __init__(self, target_to_articles):
        self._map = target_to_articles

    async def scrape_publication(self, target, limit=50, sort="new"):  # noqa: ARG002
        out = []
        for url, title in self._map.get(target, []):
            out.append(
                ScrapeResult(
                    status="success",
                    driver_used="curl_cffi",
                    article=Article(
                        url=url,
                        title=title,
                        content="A sufficiently long article body for the digest.",
                        author="Writer",
                        raw_html=f"<div>{title}</div>",
                        metadata={"bucket": "community", "source_domain": target},
                    ),
                )
            )
        return out


@pytest.mark.db
async def test_seed_to_postgres_end_to_end(db_session: AsyncSession, session_factory, monkeypatch) -> None:
    from scrapeforge.core.db.models import Source
    from scrapeforge.core.db.repositories import query_articles
    from scrapeforge.worker import community_ingest_worker
    from scrapeforge.worker.community_ingest_worker import run_community_ingest_worker
    from scrapeforge.worker.scheduler import enqueue_due_sources

    # 1. Seed two community publication sources.
    async with session_factory() as s:
        s.add_all(
            [
                Source(
                    name="substack:www.chipstrat.com",
                    bucket="community",
                    params={"url": "www.chipstrat.com", "platform": "substack", "limit": 5},
                    cron=None,
                    enabled=True,
                ),
                Source(
                    name="substack:newsletter.semianalysis.com",
                    bucket="community",
                    params={
                        "url": "newsletter.semianalysis.com",
                        "platform": "substack",
                        "limit": 5,
                    },
                    cron=None,
                    enabled=True,
                ),
            ]
        )
        await s.commit()

    # 2. Fake scraper (no network); monkeypatch the worker's platform resolver.
    fake = _FakeScraper(
        {
            "www.chipstrat.com": [("https://www.chipstrat.com/p/a", "Chip Alpha")],
            "newsletter.semianalysis.com": [
                ("https://newsletter.semianalysis.com/p/b", "Semi Beta")
            ],
        }
    )
    monkeypatch.setattr(community_ingest_worker, "_resolve_scraper", lambda platform: fake)

    queue = InMemoryMessageQueue()
    store = InMemoryObjectStore()
    settings = types.SimpleNamespace(
        JOB_QUEUE="jobs", INGEST_QUEUE="ingest", QUEUE_MAX_RETRIES=2
    )

    # 3. Scheduler enqueues two IngestMessages.
    n = await enqueue_due_sources(session_factory=session_factory, queue=queue, settings=settings)
    assert n == 2
    assert await queue.size("ingest") == 2

    # 4. Ingest worker drains the queue → articles land in Postgres.
    await run_community_ingest_worker(
        queue=queue, store=store, session_factory=session_factory, settings=settings
    )

    articles = await query_articles(db_session, bucket="community", limit=50)
    titles = {a.title for a in articles}
    assert titles == {"Chip Alpha", "Semi Beta"}
    assert all(a.author == "Writer" for a in articles)
    assert all(a.bucket == "community" for a in articles)

    # 5. Re-run scheduler + worker → no duplicate rows.
    await enqueue_due_sources(session_factory=session_factory, queue=queue, settings=settings)
    await run_community_ingest_worker(
        queue=queue, store=store, session_factory=session_factory, settings=settings
    )
    again = await query_articles(db_session, bucket="community", limit=50)
    assert len(again) == 2  # idempotent — still exactly two rows
```

- [ ] **Step 2: Run it red, then green**

Run (container + `DATABASE_URL`):
`.venv/Scripts/python.exe -m pytest tests/test_community_ingest_e2e.py -q -m db`
Expected: PASS (all earlier tasks implemented). If RED for a reason other than missing impl,
fix the offending task before continuing.

- [ ] **Step 3: Full gate**

```bash
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m ruff format --check .
# unit + @db against the container:
.venv/Scripts/python.exe -m pytest -m "not integration" -q
```
Expected: all green; coverage gate satisfied.

- [ ] **Step 4: Commit**

```bash
git add tests/test_community_ingest_e2e.py
git commit -m "test(worker): hermetic end-to-end community ingestion (seed->scheduler->ingest->PG)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Docs, invariant, and memory updates

**Files:**
- Modify: `SPEC.md` (Invariant #18 carve-out), `architecture.MD` (§7.5 + tree), `planning.MD`
- Modify: `C:\Users\nayrb\.claude\projects\C--Users-nayrb-Documents-scraper-vHezzian\memory\community-publication-fanout-gap.md` + `MEMORY.md` pointer

No code tests. Make the edits, then `ruff` is a no-op for `.md`.

- [ ] **Step 1: SPEC.md — Invariant #18 carve-out**

Find the canonical Invariant #18 statement in `SPEC.md` (search for `Invariant #18` /
the invariants list). Append this carve-out paragraph to it:

> **Community/JSON scrapers carve-out (Phase 1).** The scraper→transform claim-check split
> (stateless scraper writes raw + publishes a pointer; the transform worker is the sole
> structured writer) governs **public-bucket HTML**. Fully-parsing community scrapers (Substack;
> later Reddit) persist structured rows **within their ingestion worker**
> (`worker/community_ingest_worker.py`) via `PostgresSink`, while still archiving raw to the
> object store for claim-check/replay. Rationale: these scrapers produce complete `Article`s at
> fetch time, so a separate HTML-selector transform stage adds nothing and cannot parse their
> JSON-sourced fields. The scheduler routes such sources to the `INGEST` queue.

- [ ] **Step 2: architecture.MD**

In the module tree, add `worker/community_ingest_worker.py` and `worker/run_community_ingest.py`.
In §7.5 (or the ingestion-flow section), add the third queue + ingest stage, e.g.:

> **Community ingestion (Phase 1).** `scheduler` routes `Source` rows carrying `params.platform`
> to the `INGEST` queue; `community_ingest_worker` runs the bucket scraper's `scrape_publication`,
> archives each new post's raw payload (claim-check), and UPSERTs parsed rows via `PostgresSink`
> (idempotent). Queues: `JOB` (single-URL HTML) · `RESULTS` (HTML transform) · `INGEST`
> (community publications).

- [ ] **Step 3: planning.MD**

Mark Phase 1 community-ingestion as delivered (note: lean scheduled ingestion of the 50 curated
Substacks into Postgres; recurring per-publication fan-out via the `INGEST` queue). Add a forward
note that the next phases are per-article AI summaries → relevance ranking → swipe UI.

- [ ] **Step 4: Update the memory**

Edit `...\memory\community-publication-fanout-gap.md`: change its framing from "gap" to
"resolved (Phase 1, PR for `feat/community-ingestion`)". Keep the key fact that the *public*
HTML pipeline is single-URL, but record that **community publications now ingest via the
scheduler → INGEST queue → `community_ingest_worker`** (scrape_publication → PostgresSink), and
that per-post HTML fan-out is still not used for community sources. Update the `MEMORY.md`
pointer line to match.

- [ ] **Step 5: Commit**

```bash
git add SPEC.md architecture.MD planning.MD
git commit -m "docs: document community-ingest stage + Invariant #18 carve-out

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```
(The memory files live outside the repo; they are not committed — just updated in place.)

---

## Final verification (Definition of Done)

- [ ] `.venv/Scripts/python.exe -m ruff check .` → 0 errors
- [ ] `.venv/Scripts/python.exe -m ruff format --check .` → clean
- [ ] `.venv/Scripts/python.exe -m pytest -m "not integration" -q` (with the pgvector container +
      `DATABASE_URL`) → green incl. all new `@db` tests; coverage ≥ 80%
- [ ] `docker compose -f deployment/docker-compose.yml config` parses with the `community-ingest`
      service
- [ ] SPEC/architecture/planning + memory updated
- [ ] Push branch `feat/community-ingestion`, open PR → `main`, CI green, squash-merge. Never push
      `main`.
- [ ] Live `@integration` smoke (manual, optional): seed a real Source and run the worker against
      a live publication to confirm end-to-end before relying on the daily schedule.

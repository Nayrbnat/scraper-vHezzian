# Lean Render + Neon Live Deployment — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take the existing Substack→summary→relevance→email loop live as **3 run-once Render Cron Jobs** against a **Neon** Postgres (no Redis/MinIO), and prove the full loop ends in a real email.

**Architecture:** A new `pipeline` Typer sub-app exposes run-once `init-db` / `ingest` / `summarize` commands (the third job is the existing `digest send --source postgres`). Each is a `scrapeforge …` command that connects to Neon, does its batch, exits. `render.yaml` schedules them with secrets as env-var *names* only. `make_engine` gains an opt-in SSL path for Neon.

**Tech Stack:** Python 3.12 async, SQLAlchemy 2.0 async + asyncpg + pgvector, Typer, Render Cron Jobs (Docker), Neon serverless Postgres, pytest (`asyncio_mode=auto`), ruff. `@db` tests use an ephemeral pgvector container.

**Reference spec:** `docs/superpowers/specs/2026-06-24-render-neon-deploy-design.md`

---

## Conventions for every task

- **TDD:** failing test → red → minimal impl → green → commit.
- **Gate before each commit:** `.venv/Scripts/python.exe -m ruff format <files>` then `ruff check <files>` (0 errors).
- **`@db` tests** need pgvector. Start once + export:
  ```bash
  docker run -d --rm --name sf-pg -e POSTGRES_USER=scrapeforge -e POSTGRES_PASSWORD=scrapeforge \
    -e POSTGRES_DB=scrapeforge -p 5439:5432 pgvector/pgvector:pg16
  export DATABASE_URL="postgresql+asyncpg://scrapeforge:scrapeforge@localhost:5439/scrapeforge"
  ```
- **Commit footer:** `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Secrets:** never commit a real key/DSN. Before each `git add`, grep the staged diff for secret-looking strings.
- **Never** edit `engine.py`, `registry.py`, `repositories.py`, `exceptions.py`, the existing scraper/transform/ingest/summarize workers, or the queue/objectstore modules.

## File map

| Task | Files |
|---|---|
| 1 | `scrapeforge/pipeline/{__init__,jobs,cli}.py` (new) + `scrapeforge/cli.py` (+1 `add_typer`); `tests/test_pipeline_init_db.py` |
| 2 | `scrapeforge/pipeline/jobs.py` (+`ingest_publications`), `pipeline/cli.py` (+`ingest`); `tests/test_pipeline_ingest.py` |
| 3 | `scrapeforge/pipeline/cli.py` (+`summarize`); `tests/test_pipeline_summarize.py` |
| 4 | `scrapeforge/core/db/session.py` (opt-in SSL); `tests/test_engine_ssl.py` |
| 5 | `render.yaml` (new), `DEPLOYMENT.md` (rewrite); `tests/test_render_yaml.py` |
| 6 | `tests/test_no_raw_sql.py` (SQLi audit), `SPEC.md`/`architecture.MD`/`planning.MD`, memory |

---

## Task 1: `pipeline` sub-app + `init-db`

**Files:**
- Create: `scrapeforge/pipeline/__init__.py`, `scrapeforge/pipeline/jobs.py`, `scrapeforge/pipeline/cli.py`
- Modify: `scrapeforge/cli.py` (mount the sub-app)
- Test: `tests/test_pipeline_init_db.py`

- [ ] **Step 1: Write the failing test**

`tests/test_pipeline_init_db.py`:
```python
"""@db: init_db is idempotent and produces the articles schema (+ relevance/summary cols)."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.db
async def test_init_db_idempotent_and_creates_schema(_db_url) -> None:
    from scrapeforge.pipeline.jobs import init_db

    engine = create_async_engine(_db_url, echo=False)
    try:
        await init_db(engine)
        await init_db(engine)  # idempotent — must not raise

        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
            cols = await conn.run_sync(
                lambda c: {col["name"] for col in inspect(c).get_columns("articles")}
            )
            ext = (
                await conn.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'"))
            ).first()
        assert "articles" in tables
        assert {"relevance", "summary"} <= cols
        assert ext is not None  # pgvector enabled
    finally:
        await engine.dispose()


def test_pipeline_subapp_mounted() -> None:
    from typer.testing import CliRunner

    from scrapeforge.cli import app

    result = CliRunner().invoke(app, ["pipeline", "--help"])
    assert result.exit_code == 0
    assert "init-db" in result.stdout
```

- [ ] **Step 2: Run red** (container + `DATABASE_URL`):
`.venv/Scripts/python.exe -m pytest tests/test_pipeline_init_db.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement**

`scrapeforge/pipeline/__init__.py`:
```python
"""Run-once pipeline jobs for scheduled (cron) deployment — init-db, ingest, summarize."""
```

`scrapeforge/pipeline/jobs.py`:
```python
"""Testable run-once job functions (no Typer): schema init + Substack ingest.

These are the deploy/cron jobs in pure-async form so they can be unit-tested with injected
fakes; ``pipeline/cli.py`` wraps them with the real adapters.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from scrapeforge.core.db.migrations import ensure_summary_columns
from scrapeforge.core.db.models import Base

log = logging.getLogger(__name__)


async def init_db(engine: AsyncEngine) -> None:
    """Idempotently prepare a database: pgvector extension + tables + Phase-2 columns.

    Safe to re-run. Mirrors the test harness's schema bootstrap so a fresh Neon DB is ready.
    """
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    await ensure_summary_columns(engine)
    log.info("init_db: schema ready (vector ext + tables + summary columns)")
```

`scrapeforge/pipeline/cli.py`:
```python
"""Typer sub-app for run-once pipeline jobs (cron/deploy).

Mounted in the root CLI; invoked as ``scrapeforge pipeline <cmd>`` (the Render cron command).
Only ``asyncio.run`` appears here — the CLI is the sanctioned off-loop entry (Invariant #12).
"""

from __future__ import annotations

import asyncio
import sys

import typer

pipeline_app = typer.Typer(help="Run-once pipeline jobs for scheduled deployment.")


def _use_selector_loop() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pipeline_app.command("init-db")
def init_db_cmd() -> None:
    """Prepare the database (pgvector + tables + columns). Idempotent — run once on first deploy."""
    _use_selector_loop()
    from scrapeforge.config.settings import Settings
    from scrapeforge.core.db.session import make_engine
    from scrapeforge.pipeline.jobs import init_db

    async def _run() -> None:
        engine = make_engine(Settings().DATABASE_URL)
        try:
            await init_db(engine)
        finally:
            await engine.dispose()

    asyncio.run(_run())
    typer.echo("init-db: schema ready.")
```

In `scrapeforge/cli.py`, add the import + mount (next to the other `add_typer` lines):
```python
from scrapeforge.pipeline.cli import pipeline_app
...
app.add_typer(pipeline_app, name="pipeline")
```

- [ ] **Step 4: Run green** — `@db` test + the mount test pass.

- [ ] **Step 5: Gate + commit**
```bash
.venv/Scripts/python.exe -m ruff format scrapeforge/pipeline/ scrapeforge/cli.py tests/test_pipeline_init_db.py
.venv/Scripts/python.exe -m ruff check scrapeforge/pipeline/ scrapeforge/cli.py tests/test_pipeline_init_db.py
git add scrapeforge/pipeline/ scrapeforge/cli.py tests/test_pipeline_init_db.py
git commit -m "feat(pipeline): pipeline sub-app + idempotent init-db (pgvector + schema)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `pipeline ingest` (scrape 50 → Postgres, no queue/MinIO)

**Files:**
- Modify: `scrapeforge/pipeline/jobs.py` (+`ingest_publications`), `scrapeforge/pipeline/cli.py` (+`ingest`)
- Test: `tests/test_pipeline_ingest.py`

- [ ] **Step 1: Write the failing test**

`tests/test_pipeline_ingest.py`:
```python
"""@db: ingest_publications scrapes via the injected scraper and UPSERTs into Postgres."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scrapeforge.core.db.models import Article as ArticleRow
from scrapeforge.core.db.session import make_sessionmaker
from scrapeforge.core.models import Article, ScrapeResult


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(create_async_engine(_db_url, echo=False))


class _FakeSub:
    """Stands in for SubstackSource (only .base is used)."""

    def __init__(self, base: str) -> None:
        self.base = base


class _FakeScraper:
    def __init__(self, by_target):
        self._by = by_target

    async def scrape_publication(self, target, limit=50, sort="new"):  # noqa: ARG002
        out = []
        for url, title in self._by.get(target, []):
            out.append(
                ScrapeResult(
                    status="success", driver_used="curl_cffi",
                    article=Article(url=url, title=title, content="Body.",
                                    metadata={"bucket": "community", "source_domain": target}),
                )
            )
        out.append(ScrapeResult(status="error", driver_used="curl_cffi", article=None, error="paywalled"))
        return out


@pytest.mark.db
async def test_ingest_publications_upserts(db_session, session_factory) -> None:
    from scrapeforge.pipeline.jobs import ingest_publications

    scraper = _FakeScraper({
        "a.com": [("https://a.com/p/1", "A1")],
        "b.com": [("https://b.com/p/1", "B1"), ("https://b.com/p/2", "B2")],
    })
    sources = [_FakeSub("a.com"), _FakeSub("b.com")]

    n = await ingest_publications(session_factory=session_factory, scraper=scraper, sources=sources, limit=5)
    assert n == 3  # paywalled error skipped

    total = await db_session.scalar(select(func.count()).select_from(ArticleRow))
    assert total == 3
    # idempotent re-run → no dup rows
    await ingest_publications(session_factory=session_factory, scraper=scraper, sources=sources, limit=5)
    total2 = await db_session.scalar(select(func.count()).select_from(ArticleRow))
    assert total2 == 3
```

- [ ] **Step 2: Run red** (container + `DATABASE_URL`):
`.venv/Scripts/python.exe -m pytest tests/test_pipeline_ingest.py -q` → FAIL.

- [ ] **Step 3: Implement** — add to `scrapeforge/pipeline/jobs.py`:
```python
from scrapeforge.core.storage.postgres import PostgresSink


async def ingest_publications(*, session_factory, scraper, sources, limit: int) -> int:
    """Scrape each publication and UPSERT its successful articles into Postgres.

    Lean path — no queue, no object store. Reuses ``scrape_publication`` + ``PostgresSink``
    (idempotent UPSERT on sha256(url)). Returns the number of articles persisted.
    """
    sink = PostgresSink(session_factory)
    persisted = 0
    for source in sources:
        results = await scraper.scrape_publication(source.base, limit=limit)
        for result in results:
            if result.status != "success" or result.article is None:
                continue
            if sink.seen(result.article.url):
                continue
            await sink.write(result)
            persisted += 1
    log.info("ingest: persisted %d articles across %d publications", persisted, len(sources))
    return persisted
```
Add the `ingest` command to `scrapeforge/pipeline/cli.py`:
```python
@pipeline_app.command("ingest")
def ingest_cmd(
    limit: int = typer.Option(25, "--limit", "-l", help="Max posts per publication"),
    sector: str | None = typer.Option(None, "--sector", "-s", help="Only this sector"),
    max_pubs: int | None = typer.Option(None, "--max", "-m", help="Cap number of publications"),
) -> None:
    """Scrape the curated Substacks straight into Postgres (no Redis/MinIO)."""
    _use_selector_loop()
    from scrapeforge.config.settings import Settings
    from scrapeforge.core.db.session import make_engine, make_sessionmaker
    from scrapeforge.pipeline.jobs import ingest_publications
    from scrapeforge.scrapers.community.substack import SubstackScraper
    from scrapeforge.scrapers.community.substack_sources import select_sources

    sources = select_sources(sector=sector, limit=max_pubs)

    async def _run() -> int:
        engine = make_engine(Settings().DATABASE_URL)
        try:
            return await ingest_publications(
                session_factory=make_sessionmaker(engine),
                scraper=SubstackScraper(), sources=sources, limit=limit,
            )
        finally:
            await engine.dispose()

    n = asyncio.run(_run())
    typer.echo(f"ingest: persisted {n} articles from {len(sources)} publication(s).")
```

- [ ] **Step 4: Run green** — `@db` test passes.

- [ ] **Step 5: Gate + commit**
```bash
.venv/Scripts/python.exe -m ruff format scrapeforge/pipeline/ tests/test_pipeline_ingest.py
.venv/Scripts/python.exe -m ruff check scrapeforge/pipeline/ tests/test_pipeline_ingest.py
git add scrapeforge/pipeline/jobs.py scrapeforge/pipeline/cli.py tests/test_pipeline_ingest.py
git commit -m "feat(pipeline): run-once ingest (scrape 50 Substacks -> PostgresSink, no queue/MinIO)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `pipeline summarize` (run-once drain)

**Files:**
- Modify: `scrapeforge/pipeline/cli.py` (+`summarize`)
- Test: `tests/test_pipeline_summarize.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_pipeline_summarize.py`:
```python
"""The summarize command is registered and idles cleanly without a key (no spend)."""

from __future__ import annotations


def test_summarize_registered() -> None:
    from typer.testing import CliRunner

    from scrapeforge.cli import app

    result = CliRunner().invoke(app, ["pipeline", "--help"])
    assert result.exit_code == 0 and "summarize" in result.stdout


def test_summarize_no_key_is_noop(fake_env, monkeypatch) -> None:
    """With an empty SUMMARY_API_KEY the command exits cleanly without constructing the LLM or DB.

    The guard returns BEFORE any engine/LLM is built, so a spy on the (lazily-imported)
    summarizer must never be called.
    """
    from typer.testing import CliRunner

    from scrapeforge.cli import app

    monkeypatch.setenv("SUMMARY_API_KEY", "")

    constructed = []
    monkeypatch.setattr(
        "scrapeforge.core.llm.openai_compatible.OpenAICompatibleSummarizer",
        lambda *a, **k: constructed.append(1),
    )

    result = CliRunner().invoke(app, ["pipeline", "summarize"])
    assert result.exit_code == 0
    assert "skipped" in result.stdout
    assert constructed == []  # never built the LLM (early return before the lazy import)
```
> Note: the command checks `SummarizerSettings().SUMMARY_API_KEY` and returns immediately (echoing
> "skipped") when empty — before importing/building the engine or `OpenAICompatibleSummarizer` —
> mirroring `worker/run_summarize.py`. So this test needs no DB and no `_run_summarize` helper.

- [ ] **Step 2: Run red** — `.venv/Scripts/python.exe -m pytest tests/test_pipeline_summarize.py -q` → FAIL.

- [ ] **Step 3: Implement** — add to `scrapeforge/pipeline/cli.py`:
```python
@pipeline_app.command("summarize")
def summarize_cmd() -> None:
    """Summarize + score all un-summarized articles once, then exit (run-once drain)."""
    _use_selector_loop()
    import logging

    from scrapeforge.core.llm.settings import SummarizerSettings

    settings = SummarizerSettings()
    if not settings.SUMMARY_API_KEY:
        logging.getLogger(__name__).warning("SUMMARY_API_KEY empty — summarize skipped (set it to enable).")
        typer.echo("summarize: skipped (no SUMMARY_API_KEY).")
        return

    from scrapeforge.config.settings import Settings
    from scrapeforge.core.db.migrations import ensure_summary_columns
    from scrapeforge.core.db.session import make_engine, make_sessionmaker
    from scrapeforge.core.llm.openai_compatible import OpenAICompatibleSummarizer
    from scrapeforge.worker.summarize_worker import run_summarize_worker

    async def _run() -> None:
        engine = make_engine(Settings().DATABASE_URL)
        try:
            await ensure_summary_columns(engine)  # self-heal columns on an existing DB
            await run_summarize_worker(
                session_factory=make_sessionmaker(engine),
                summarizer=OpenAICompatibleSummarizer(settings), settings=settings,
            )
        finally:
            await engine.dispose()

    asyncio.run(_run())
    typer.echo("summarize: done.")
```

- [ ] **Step 4: Run green** — tests pass.

- [ ] **Step 5: Gate + commit**
```bash
.venv/Scripts/python.exe -m ruff format scrapeforge/pipeline/cli.py tests/test_pipeline_summarize.py
.venv/Scripts/python.exe -m ruff check scrapeforge/pipeline/cli.py tests/test_pipeline_summarize.py
git add scrapeforge/pipeline/cli.py tests/test_pipeline_summarize.py
git commit -m "feat(pipeline): run-once summarize (drain WHERE summary IS NULL, idle without key)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Neon SSL in `make_engine`

**Files:**
- Modify: `scrapeforge/core/db/session.py`
- Test: `tests/test_engine_ssl.py`

- [ ] **Step 1: Write the failing test**

`tests/test_engine_ssl.py`:
```python
"""make_engine enables SSL (for Neon) only when DATABASE_SSL is set — local stays plain."""

from __future__ import annotations


def test_ssl_connect_args_opt_in(monkeypatch) -> None:
    from scrapeforge.core.db.session import _ssl_connect_args

    monkeypatch.delenv("DATABASE_SSL", raising=False)
    assert _ssl_connect_args() == {}

    monkeypatch.setenv("DATABASE_SSL", "require")
    assert _ssl_connect_args() == {"ssl": True}

    monkeypatch.setenv("DATABASE_SSL", "false")
    assert _ssl_connect_args() == {}


def test_make_engine_uses_ssl_args(monkeypatch) -> None:
    from scrapeforge.core.db import session as sess

    captured = {}

    def _fake_create(url, **kw):
        captured.update(kw)
        return object()

    monkeypatch.setattr(sess, "create_async_engine", _fake_create)
    monkeypatch.setenv("DATABASE_SSL", "require")
    sess.make_engine("postgresql+asyncpg://u:p@h/db")
    assert captured.get("connect_args") == {"ssl": True}
```

- [ ] **Step 2: Run red** — `.venv/Scripts/python.exe -m pytest tests/test_engine_ssl.py -q` → FAIL.

- [ ] **Step 3: Implement** — in `scrapeforge/core/db/session.py`, add near the top:
```python
import os


def _ssl_connect_args() -> dict:
    """Return asyncpg SSL connect-args when DATABASE_SSL opts in (Neon); empty otherwise.

    Local/CI Postgres has no TLS, so SSL stays OFF unless DATABASE_SSL is set to a truthy value
    (``require``/``true``/``1``). Set ``DATABASE_SSL=require`` on Render for Neon.
    """
    if os.environ.get("DATABASE_SSL", "").strip().lower() in {"require", "true", "1"}:
        return {"ssl": True}
    return {}
```
And in `make_engine`, pass it:
```python
    return create_async_engine(
        database_url,
        echo=False,
        pool_pre_ping=True,
        pool_recycle=3600,
        connect_args=_ssl_connect_args(),
    )
```

- [ ] **Step 4: Run green** — tests pass. Also confirm no regression: `@db` tests still connect
  locally (DATABASE_SSL unset → plain): `.venv/Scripts/python.exe -m pytest tests/test_pipeline_ingest.py -q -m db`.

- [ ] **Step 5: Gate + commit**
```bash
.venv/Scripts/python.exe -m ruff format scrapeforge/core/db/session.py tests/test_engine_ssl.py
.venv/Scripts/python.exe -m ruff check scrapeforge/core/db/session.py tests/test_engine_ssl.py
git add scrapeforge/core/db/session.py tests/test_engine_ssl.py
git commit -m "feat(db): opt-in SSL connect-args for make_engine (Neon via DATABASE_SSL)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `render.yaml` + `DEPLOYMENT.md`

**Files:**
- Create: `render.yaml`
- Rewrite: `DEPLOYMENT.md`
- Test: `tests/test_render_yaml.py`

- [ ] **Step 1: Write the failing test**

`tests/test_render_yaml.py`:
```python
"""render.yaml is valid, defines the cron jobs, and carries NO inline secret values."""

from __future__ import annotations

from pathlib import Path

import yaml

_SECRET_NAMES = {
    "DATABASE_URL", "STATE_STORE_KEY", "SUMMARY_API_KEY",
    "DIGEST_SMTP_PASSWORD", "DIGEST_SMTP_USER", "DIGEST_TO",
}


def test_render_yaml_valid_and_secretless() -> None:
    doc = yaml.safe_load(Path("render.yaml").read_text(encoding="utf-8"))
    services = doc["services"]
    names = {s["name"] for s in services}
    # the three scheduled jobs + init-db are present
    assert {"ingest", "summarize", "digest"} <= names
    for svc in services:
        assert svc["type"] == "cron"
        for ev in svc.get("envVars", []):
            # secrets must be declared by name only (sync: false), never an inline value
            if ev["key"] in _SECRET_NAMES:
                assert ev.get("sync") is False, f"{ev['key']} must be sync:false (dashboard secret)"
                assert "value" not in ev, f"{ev['key']} must NOT have an inline value"
```

- [ ] **Step 2: Run red** — `.venv/Scripts/python.exe -m pytest tests/test_render_yaml.py -q` → FAIL (no render.yaml).

- [ ] **Step 3: Implement `render.yaml`** (Render Blueprint; secrets are `sync: false`, schedules UTC):
```yaml
# Render Blueprint — lean Substack pipeline as run-once cron jobs against Neon.
# Secrets (sync:false) are set in the Render dashboard, NEVER here. See DEPLOYMENT.md.
services:
  - type: cron
    name: ingest
    runtime: docker
    dockerfilePath: deployment/Dockerfile.api
    dockerContext: .
    schedule: "0 6 * * *" # 06:00 UTC daily — scrape 50 Substacks into Neon
    dockerCommand: scrapeforge pipeline ingest
    envVars: &pipeline-env
      - { key: DATABASE_URL, sync: false }
      - { key: DATABASE_SSL, value: require }
      - { key: STATE_STORE_KEY, sync: false }
      - { key: SUMMARY_API_KEY, sync: false }
      - { key: SUMMARY_MODEL, value: glm-4.5-flash }
      - { key: SUMMARY_API_BASE_URL, value: https://api.z.ai/api/paas/v4 }
      - { key: SUMMARY_PORTFOLIO, sync: false }
      - { key: SUMMARY_INTERESTS, sync: false }
      - { key: DIGEST_SMTP_HOST, sync: false }
      - { key: DIGEST_SMTP_PORT, sync: false }
      - { key: DIGEST_SMTP_USER, sync: false }
      - { key: DIGEST_SMTP_PASSWORD, sync: false }
      - { key: DIGEST_FROM, sync: false }
      - { key: DIGEST_TO, sync: false }
      - { key: DIGEST_SOURCE, value: postgres }
  - type: cron
    name: summarize
    runtime: docker
    dockerfilePath: deployment/Dockerfile.api
    dockerContext: .
    schedule: "30 6 * * *" # 06:30 UTC — summarize + score the new articles
    dockerCommand: scrapeforge pipeline summarize
    envVars: *pipeline-env
  - type: cron
    name: digest
    runtime: docker
    dockerfilePath: deployment/Dockerfile.api
    dockerContext: .
    schedule: "0 8 * * *" # 08:00 UTC — send the real relevance-ranked email
    dockerCommand: scrapeforge digest send --yes --source postgres
    envVars: *pipeline-env
  - type: cron
    name: init-db
    runtime: docker
    dockerfilePath: deployment/Dockerfile.api
    dockerContext: .
    schedule: "0 0 31 2 *" # never auto-runs (Feb 31). Trigger MANUALLY once on first deploy.
    dockerCommand: scrapeforge pipeline init-db
    envVars: *pipeline-env
```

- [ ] **Step 4: Rewrite `DEPLOYMENT.md`** — a step-by-step runbook covering:
  1. Create a **Neon** project → copy the **pooled** connection string → convert to
     `postgresql+asyncpg://…` and set `DATABASE_URL` + `DATABASE_SSL=require`.
  2. Create a **Render** Blueprint from this repo (`render.yaml`); in the dashboard, set every
     `sync:false` secret (`DATABASE_URL`, `STATE_STORE_KEY`, `SUMMARY_API_KEY`, `SUMMARY_PORTFOLIO`,
     `SUMMARY_INTERESTS`, the `DIGEST_SMTP_*`, `DIGEST_FROM`, `DIGEST_TO`). **Never** put secrets in
     `render.yaml` or git.
  3. **Manually trigger `init-db` once.** Then trigger `ingest` → `summarize` → `digest` and
     verify: rows in Neon, summaries/scores populated, a real email in your inbox.
  4. Note: schedules are UTC (digest 08:00 UTC); adjust for BST/GMT if you want a fixed London hour.
  5. Security reminders: secrets live only in Render; rotate the Gmail app password / GLM key if
     ever exposed; the DB is reached over TLS (`DATABASE_SSL=require`).

- [ ] **Step 5: Run green + validate compose unaffected**
```bash
.venv/Scripts/python.exe -m pytest tests/test_render_yaml.py -q
.venv/Scripts/python.exe -c "import yaml; yaml.safe_load(open('render.yaml',encoding='utf-8')); print('render.yaml OK')"
```

- [ ] **Step 6: Gate + commit**
```bash
.venv/Scripts/python.exe -m ruff check tests/test_render_yaml.py
git add render.yaml DEPLOYMENT.md tests/test_render_yaml.py
git commit -m "feat(deploy): render.yaml cron blueprint (secretless) + Render/Neon runbook

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: SQLi audit + docs + memory

**Files:** `tests/test_no_raw_sql.py` (new), `SPEC.md`, `architecture.MD`, `planning.MD`, memory.

- [ ] **Step 1: Write the SQLi-audit test**

`tests/test_no_raw_sql.py`:
```python
"""Guard: no dynamically-built SQL strings in the codebase (SQL-injection safety).

All DB access must go through SQLAlchemy ORM/Core with bound parameters. The only allowed raw
SQL is STATIC DDL/maintenance via ``text("…")`` with NO f-string / % / .format interpolation.
"""

from __future__ import annotations

import re
from pathlib import Path

# Flags f-string or %/.format interpolation inside a text("...") / execute("...") call.
_DYNAMIC_SQL = re.compile(r"""(text|execute)\(\s*f["']|(text|execute)\([^)]*%[^)]*\)""")


def test_no_dynamic_sql_strings() -> None:
    offenders = []
    for path in Path("scrapeforge").rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        for m in _DYNAMIC_SQL.finditer(src):
            line = src[: m.start()].count("\n") + 1
            offenders.append(f"{path}:{line}")
    assert not offenders, "Dynamic SQL string(s) found (use bound params): " + ", ".join(offenders)
```

- [ ] **Step 2: Run it** — `.venv/Scripts/python.exe -m pytest tests/test_no_raw_sql.py -q` → expect PASS
  (the codebase already uses bound params + static DDL). If it FAILS, fix the offending query to use
  SQLAlchemy bound parameters before proceeding.

- [ ] **Step 3: Docs** — `SPEC.md`/`architecture.MD`: document the `pipeline` sub-app + the lean
  run-once cron deployment (init-db/ingest/summarize/digest) as an alternative to the always-on
  docker-compose stack; note `DATABASE_SSL` for Neon. `planning.MD`: mark "live deployment
  (Render+Neon)" as the active phase; note the swipe app + buckets remain deferred.

- [ ] **Step 4: Memory** — add a `project` note: "Live deploy = lean Render Cron Jobs (ingest →
  summarize → digest send) against Neon Postgres; no Redis/MinIO. New `pipeline` CLi
  (init-db/ingest/summarize). Secrets only in Render env (render.yaml is secretless, sync:false);
  Neon needs `DATABASE_SSL=require`. The full queue/worker docker-compose stays as a scale-out
  option." Update `MEMORY.md`.

- [ ] **Step 5: Commit**
```bash
git add tests/test_no_raw_sql.py SPEC.md architecture.MD planning.MD
git commit -m "test+docs: SQLi-audit guard + document lean Render/Neon deployment

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Final verification (Definition of Done)

**Hermetic (CI / what I build):**
- [ ] `.venv/Scripts/python.exe -m ruff check .` → 0; `ruff format --check .` → clean
- [ ] `.venv/Scripts/python.exe -m pytest -m "not integration" -q` (container + `DATABASE_URL`) →
      green incl. all new unit/`@db` tests; coverage ≥ 80%; existing suite unaffected
- [ ] `render.yaml` valid + secretless; `python -c "import scrapeforge.cli"` clean; `scrapeforge pipeline --help` lists init-db/ingest/summarize
- [ ] No secret values committed (grep the whole branch diff)
- [ ] Push `feat/render-neon-deploy`, open PR → `main`, CI green, squash-merge. Never push `main`.

**Live (manual, with the owner — the real proof):**
- [ ] Owner: create Neon DB + Render Blueprint, set the `sync:false` secrets in Render.
- [ ] Trigger `init-db` (once) → `ingest` → `summarize` → `digest`; confirm rows in Neon, scores
      populated, and a **real relevance-ranked email** arrives in `DIGEST_TO`.
- [ ] Confirm the three crons are scheduled and the next daily run produces the email automatically.
```

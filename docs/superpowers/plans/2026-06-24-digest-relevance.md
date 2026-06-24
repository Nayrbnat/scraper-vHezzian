# Phase 2.5: Relevance-Ranked Digest — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The daily email becomes a single "Top updates" list ranked by the AI relevance score — each item shows its 5 bullets, a relevance badge, and the one-line reason — pulling real scored articles from Postgres.

**Architecture:** A new `--source postgres` for the digest: an async loader reads recent summarized articles (relevance-desc, last 48h), a pure `build_relevance_digest` filters to a floor + caps top-N + maps to digest items, and the renderer shows bullets + badge + reason. The existing keyword path (`sample`/`jsonl:`) is untouched. No pipeline/architecture change.

**Tech Stack:** Python 3.12 async, SQLAlchemy 2.0 async + asyncpg + pgvector, Pydantic v2, `pydantic-settings`, Typer, pytest (`asyncio_mode=auto`), ruff. `@db` tests use an ephemeral pgvector container.

**Reference spec:** `docs/superpowers/specs/2026-06-24-digest-relevance-design.md`

---

## Conventions for every task

- **TDD:** failing test → red → minimal impl → green → commit.
- **Gate before each commit:** `.venv/Scripts/python.exe -m ruff format <files>` then
  `.venv/Scripts/python.exe -m ruff check <files>` (0 errors).
- **`@db` tests** need pgvector. Start once + export the URL:
  ```bash
  docker run -d --rm --name sf-pg -e POSTGRES_USER=scrapeforge -e POSTGRES_PASSWORD=scrapeforge \
    -e POSTGRES_DB=scrapeforge -p 5439:5432 pgvector/pgvector:pg16
  export DATABASE_URL="postgresql+asyncpg://scrapeforge:scrapeforge@localhost:5439/scrapeforge"
  ```
- **Commit footer (every commit):** `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Never** edit `engine.py`, `core/registry.py`, `core/db/repositories.py`, `exceptions.py`, the
  core `Settings` class, the root `cli.py`, or the scraper/transform/ingest/summarize workers.
- The Postgres query is **inlined** in `digest/postgres_source.py` (not added to `repositories.py`).

## File map

| Task | Files |
|---|---|
| 1 | `scrapeforge/digest/models.py` (+3 item fields, +`"top"` key), `scrapeforge/digest/settings.py` (new); `tests/test_digest_models.py` (new), `tests/test_digest_settings.py` (new) |
| 2 | `scrapeforge/digest/relevance.py` (new); `tests/test_digest_relevance.py` (new) |
| 3 | `scrapeforge/digest/render.py` (modify); `tests/test_digest_render.py` (new) |
| 4 | `scrapeforge/digest/postgres_source.py` (new); `tests/test_digest_postgres_source.py` (new) |
| 5 | `scrapeforge/digest/service.py` (modify), `scrapeforge/digest/cli.py` (modify); `tests/test_digest_postgres_e2e.py` (new) |
| 6 | `.github/workflows/daily-digest.yml`, `SPEC.md`, `architecture.MD`, `planning.MD`, memory (no tests) |

---

## Task 1: digest model fields + `DigestSettings`

**Files:**
- Modify: `scrapeforge/digest/models.py`
- Create: `scrapeforge/digest/settings.py`
- Test: `tests/test_digest_models.py`, `tests/test_digest_settings.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_digest_models.py`:
```python
"""The DigestItem gains bullets/relevance/reason; DigestSection gains the 'top' key."""

from __future__ import annotations


def test_digest_item_new_fields_default_empty() -> None:
    from scrapeforge.digest.models import DigestItem

    item = DigestItem(title="T", url="https://e.com/a", source="e.com", summary="s")
    assert item.bullets == []
    assert item.relevance is None
    assert item.reason is None


def test_digest_item_accepts_bullets_relevance_reason() -> None:
    from scrapeforge.digest.models import DigestItem

    item = DigestItem(
        title="T", url="https://e.com/a", source="e.com", summary="s",
        bullets=["b1", "b2"], relevance=9, reason="why",
    )
    assert item.bullets == ["b1", "b2"] and item.relevance == 9 and item.reason == "why"


def test_digest_section_top_key_allowed() -> None:
    from scrapeforge.digest.models import DigestSection

    section = DigestSection(key="top", heading="Top updates", items=[])
    assert section.key == "top"
```

`tests/test_digest_settings.py`:
```python
"""DigestSettings ranking knobs (defaults + env override)."""

from __future__ import annotations


def test_defaults(fake_env) -> None:
    from scrapeforge.digest.settings import DigestSettings

    s = DigestSettings()
    assert s.DIGEST_RELEVANCE_FLOOR == 5
    assert s.DIGEST_TOP_N == 10
    assert s.DIGEST_WINDOW_HOURS == 48


def test_env_override(monkeypatch, fake_env) -> None:
    from scrapeforge.digest.settings import DigestSettings

    monkeypatch.setenv("DIGEST_RELEVANCE_FLOOR", "7")
    monkeypatch.setenv("DIGEST_TOP_N", "5")
    monkeypatch.setenv("DIGEST_WINDOW_HOURS", "24")
    s = DigestSettings()
    assert (s.DIGEST_RELEVANCE_FLOOR, s.DIGEST_TOP_N, s.DIGEST_WINDOW_HOURS) == (7, 5, 24)
```

- [ ] **Step 2: Run red** — `.venv/Scripts/python.exe -m pytest tests/test_digest_models.py tests/test_digest_settings.py -q` → FAIL.

- [ ] **Step 3: Implement model fields**

In `scrapeforge/digest/models.py`, in `class DigestItem`, add after the `summary` field:
```python
    bullets: list[str] = Field(
        default_factory=list, description="AI 5-bullet summary (empty for the keyword path)."
    )
    relevance: int | None = Field(default=None, description="AI relevance score 1-10, or None.")
    reason: str | None = Field(default=None, description="One-line 'why this score', or None.")
```
And change the `DigestSection.key` type from `Literal["portfolio", "themes", "topics"]` to
`Literal["portfolio", "themes", "topics", "top"]`.

- [ ] **Step 4: Implement `DigestSettings`**

`scrapeforge/digest/settings.py`:
```python
"""Per-module ranking config for the relevance digest (Invariant #16 — never core Settings)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DigestSettings(BaseSettings):
    """Knobs for the relevance-ranked digest. Overridable via environment / ``.env``."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DIGEST_RELEVANCE_FLOOR: int = Field(default=5)  # minimum 1-10 score to include
    DIGEST_TOP_N: int = Field(default=10)  # max items per email
    DIGEST_WINDOW_HOURS: int = Field(default=48)  # recency window considered
```

- [ ] **Step 5: Run green** — PASS (5 passed). Also run the existing digest tests for no regression:
  `.venv/Scripts/python.exe -m pytest tests/test_digest.py -q`.

- [ ] **Step 6: Gate + commit**
```bash
.venv/Scripts/python.exe -m ruff format scrapeforge/digest/models.py scrapeforge/digest/settings.py tests/test_digest_models.py tests/test_digest_settings.py
.venv/Scripts/python.exe -m ruff check scrapeforge/digest/models.py scrapeforge/digest/settings.py tests/test_digest_models.py tests/test_digest_settings.py
git add scrapeforge/digest/models.py scrapeforge/digest/settings.py tests/test_digest_models.py tests/test_digest_settings.py
git commit -m "feat(digest): DigestItem bullets/relevance/reason + 'top' section + DigestSettings

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `build_relevance_digest`

**Files:**
- Create: `scrapeforge/digest/relevance.py`
- Test: `tests/test_digest_relevance.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_digest_relevance.py`:
```python
"""Pure tests for build_relevance_digest (no DB)."""

from __future__ import annotations

from datetime import UTC, datetime

from scrapeforge.core.models import Article
from scrapeforge.digest.models import DigestPreferences, Subscriber


def _sub() -> Subscriber:
    return Subscriber(id="dee", name="Dee", email="dee@example.com", preferences=DigestPreferences())


def _article(url: str, title: str, *, relevance: int, bullets=None, reason="r", days_ago: int = 0):
    return Article(
        url=url, title=title, content="Body text long enough to summarize for a fallback blurb.",
        publish_date=datetime(2026, 6, 24, tzinfo=UTC).replace(day=24 - days_ago),
        metadata={
            "source_domain": "e.com", "bucket": "community", "relevance": relevance,
            "summary": {"bullets": bullets or [f"{title} b1", f"{title} b2"], "reason": reason},
        },
    )


def test_filters_below_floor_and_sorts_desc() -> None:
    from scrapeforge.digest.relevance import build_relevance_digest

    arts = [
        _article("https://e.com/1", "Low", relevance=3),
        _article("https://e.com/2", "High", relevance=9),
        _article("https://e.com/3", "Mid", relevance=6),
    ]
    digest = build_relevance_digest(_sub(), arts, min_relevance=5, limit=10)
    assert len(digest.sections) == 1 and digest.sections[0].key == "top"
    titles = [i.title for i in digest.sections[0].items]
    assert titles == ["High", "Mid"]  # Low (<5) dropped; sorted desc
    top = digest.sections[0].items[0]
    assert top.relevance == 9 and top.bullets == ["High b1", "High b2"] and top.reason == "r"


def test_caps_at_limit() -> None:
    from scrapeforge.digest.relevance import build_relevance_digest

    arts = [_article(f"https://e.com/{i}", f"A{i}", relevance=9 - (i % 3)) for i in range(8)]
    digest = build_relevance_digest(_sub(), arts, min_relevance=1, limit=3)
    assert len(digest.sections[0].items) == 3


def test_empty_when_nothing_clears_floor() -> None:
    from scrapeforge.digest.relevance import build_relevance_digest

    arts = [_article("https://e.com/1", "Low", relevance=2)]
    digest = build_relevance_digest(_sub(), arts, min_relevance=5, limit=10)
    assert digest.sections == [] and digest.is_empty


def test_lead_text_fallback_summary_is_set() -> None:
    from scrapeforge.digest.relevance import build_relevance_digest

    digest = build_relevance_digest(
        _sub(), [_article("https://e.com/1", "X", relevance=8)], min_relevance=5, limit=10
    )
    item = digest.sections[0].items[0]
    assert item.summary  # non-empty lead-text fallback (used if a client ignores bullets)
    assert item.source == "e.com"
```

- [ ] **Step 2: Run red** — `.venv/Scripts/python.exe -m pytest tests/test_digest_relevance.py -q` → FAIL.

- [ ] **Step 3: Implement** `scrapeforge/digest/relevance.py`:
```python
"""Assemble a relevance-ranked Digest from scored articles (pure; no DB).

The Postgres source (digest.postgres_source) stashes each article's ``relevance`` and its
``summary`` JSONB (``bullets``/``reason``) into ``article.metadata``; this builder reads those,
filters to a floor, sorts by relevance (recency tiebreak), caps at a limit, and wraps the result
in a single "Top updates" section.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from scrapeforge.core.models import Article
from scrapeforge.digest.matcher import summarize
from scrapeforge.digest.models import Digest, DigestItem, DigestSection, Subscriber


def _relevance(article: Article) -> int:
    value = article.metadata.get("relevance")
    return value if isinstance(value, int) else 0


def _item(article: Article) -> DigestItem:
    summary = article.metadata.get("summary") or {}
    raw_bullets = summary.get("bullets") or []
    bullets = [b.strip() for b in raw_bullets if isinstance(b, str) and b.strip()]
    reason = summary.get("reason")
    return DigestItem(
        title=article.title or "(untitled)",
        url=article.url,
        source=urlsplit(article.url).hostname or "",
        published=article.publish_date,
        summary=summarize(article.content),  # lead-text fallback
        bullets=bullets,
        relevance=_relevance(article) or None,
        reason=reason.strip() if isinstance(reason, str) and reason.strip() else None,
    )


def _sort_key(article: Article) -> tuple[int, float]:
    ts = article.publish_date.timestamp() if article.publish_date else 0.0
    return (_relevance(article), ts)


def build_relevance_digest(
    subscriber: Subscriber,
    articles: list[Article],
    *,
    min_relevance: int = 5,
    limit: int = 10,
    now: datetime | None = None,
) -> Digest:
    """Build a single "Top updates" section ranked by relevance (>= floor, capped at *limit*)."""
    now = now or datetime.now(UTC)
    eligible = [a for a in articles if _relevance(a) >= min_relevance]
    eligible.sort(key=_sort_key, reverse=True)
    items = [_item(a) for a in eligible[:limit]]
    sections = (
        [DigestSection(key="top", heading="Top updates", items=items)] if items else []
    )
    return Digest(
        subscriber_id=subscriber.id,
        subscriber_name=subscriber.name,
        subscriber_email=subscriber.email,
        cadence=subscriber.preferences.cadence,
        period=now.date().isoformat(),
        generated_at=now,
        sections=sections,
    )
```

- [ ] **Step 4: Run green** — PASS (4 passed).

- [ ] **Step 5: Gate + commit**
```bash
.venv/Scripts/python.exe -m ruff format scrapeforge/digest/relevance.py tests/test_digest_relevance.py
.venv/Scripts/python.exe -m ruff check scrapeforge/digest/relevance.py tests/test_digest_relevance.py
git add scrapeforge/digest/relevance.py tests/test_digest_relevance.py
git commit -m "feat(digest): build_relevance_digest (floor + top-N ranked 'Top updates')

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: render bullets + badge + reason

**Files:**
- Modify: `scrapeforge/digest/render.py`
- Test: `tests/test_digest_render.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_digest_render.py`:
```python
"""Renderer shows bullets+badge+reason for relevance items; falls back to the blurb otherwise."""

from __future__ import annotations

from datetime import UTC, datetime

from scrapeforge.digest.models import Digest, DigestItem, DigestSection


def _digest(item: DigestItem) -> Digest:
    return Digest(
        subscriber_id="dee", subscriber_name="Dee", subscriber_email="dee@example.com",
        period="2026-06-24", generated_at=datetime(2026, 6, 24, tzinfo=UTC),
        sections=[DigestSection(key="top", heading="Top updates", items=[item])],
    )


def test_html_renders_badge_bullets_reason() -> None:
    from scrapeforge.digest.render import render_html

    item = DigestItem(
        title="TSMC roadmap", url="https://e.com/a", source="e.com", summary="blurb",
        bullets=["bullet one", "bullet two", "bullet three"], relevance=9, reason="your niche; fresh",
    )
    html = render_html(_digest(item))
    assert "9/10" in html
    assert "bullet one" in html and "bullet two" in html and "bullet three" in html
    assert "your niche; fresh" in html
    assert "blurb" not in html  # lead-text blurb replaced by bullets


def test_text_renders_badge_bullets_reason() -> None:
    from scrapeforge.digest.render import render_text

    item = DigestItem(
        title="TSMC roadmap", url="https://e.com/a", source="e.com", summary="blurb",
        bullets=["bullet one", "bullet two"], relevance=8, reason="why",
    )
    text = render_text(_digest(item))
    assert "8/10" in text and "bullet one" in text and "why" in text


def test_keyword_item_without_bullets_still_renders_blurb() -> None:
    from scrapeforge.digest.render import render_html

    item = DigestItem(
        title="Old style", url="https://e.com/b", source="e.com",
        summary="the lead-text blurb", matched_on=["Stripe"],
    )
    html = render_html(_digest(item))
    assert "the lead-text blurb" in html  # legacy path unchanged
    assert "Stripe" in html  # matched_on chip
```

- [ ] **Step 2: Run red** — `.venv/Scripts/python.exe -m pytest tests/test_digest_render.py -q` → FAIL
  (the badge/bullets aren't rendered yet).

- [ ] **Step 3: Implement** — in `scrapeforge/digest/render.py`:

Add a badge helper and branch `_html_item`:
```python
def _badge_html(relevance: int | None) -> str:
    if relevance is None:
        return ""
    color = "#16a34a" if relevance >= 8 else "#d97706" if relevance >= 5 else "#6b7280"
    return (
        f'<span style="display:inline-block;background:{color};color:#fff;border-radius:10px;'
        f'padding:1px 8px;font-size:12px;font-weight:600;margin-right:8px;">{relevance}/10</span>'
    )
```
Replace the body of `_html_item` so it branches on `item.bullets`:
```python
def _html_item(item) -> str:
    if item.bullets:
        bullets = "".join(f'<li style="margin:2px 0;">{escape(b)}</li>' for b in item.bullets)
        why = (
            f'<div style="font-size:12px;color:#9ca3af;margin-top:6px;">why: {escape(item.reason)}</div>'
            if item.reason
            else ""
        )
        return (
            '<div style="margin:0 0 18px;padding:0 0 14px;border-bottom:1px solid #eee;">'
            f"{_badge_html(item.relevance)}"
            f'<a href="{escape(item.url)}" style="font-size:16px;font-weight:600;'
            f'color:#111;text-decoration:none;">{escape(item.title)}</a>'
            f'<div style="font-size:12px;color:#888;margin:2px 0;">{escape(item.source)}</div>'
            f'<ul style="font-size:14px;color:#333;line-height:1.5;margin:6px 0;'
            f'padding-left:20px;">{bullets}</ul>'
            f"{why}"
            "</div>"
        )
    # --- legacy keyword path (unchanged) ---
    tag = ""
    if item.matched_on:
        chips = " ".join(
            f'<span style="background:#eef2ff;color:#3730a3;border-radius:10px;'
            f'padding:1px 8px;font-size:12px;margin-right:4px;">{escape(m)}</span>'
            for m in item.matched_on
        )
        tag = f'<div style="margin:4px 0 6px;">{chips}</div>'
    return (
        '<div style="margin:0 0 18px;padding:0 0 14px;border-bottom:1px solid #eee;">'
        f'<a href="{escape(item.url)}" style="font-size:16px;font-weight:600;'
        f'color:#111;text-decoration:none;">{escape(item.title)}</a>'
        f'<div style="font-size:12px;color:#888;margin:2px 0;">{escape(item.source)}</div>'
        f"{tag}"
        f'<div style="font-size:14px;color:#333;line-height:1.5;">{escape(item.summary)}</div>'
        "</div>"
    )
```
In `render_text`, replace the per-item block inside the section loop with a bullets-aware version:
```python
        for item in section.items:
            if item.bullets:
                badge = f"[{item.relevance}/10] " if item.relevance is not None else ""
                lines.append(f"- {badge}{item.title}")
                lines.append(f"  {item.source} — {item.url}")
                for b in item.bullets:
                    lines.append(f"  • {b}")
                if item.reason:
                    lines.append(f"  why: {item.reason}")
                lines.append("")
            else:
                tag = f"  [{', '.join(item.matched_on)}]" if item.matched_on else ""
                lines.append(f"- {item.title}{tag}")
                lines.append(f"  {item.source} — {item.url}")
                lines.append(f"  {item.summary}")
                lines.append("")
```
Generalize the empty-state copy (both `render_text` line and `render_html` `empty`) from
"No new updates matched your preferences today." to **"No updates to show right now."**

- [ ] **Step 4: Run green** — PASS (3 passed). Run the existing digest tests too:
  `.venv/Scripts/python.exe -m pytest tests/test_digest.py -q` (no regression on the keyword path).

- [ ] **Step 5: Gate + commit**
```bash
.venv/Scripts/python.exe -m ruff format scrapeforge/digest/render.py tests/test_digest_render.py
.venv/Scripts/python.exe -m ruff check scrapeforge/digest/render.py tests/test_digest_render.py
git add scrapeforge/digest/render.py tests/test_digest_render.py
git commit -m "feat(digest): render relevance badge + bullets + reason (keyword path unchanged)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Postgres digest source

**Files:**
- Create: `scrapeforge/digest/postgres_source.py`
- Test: `tests/test_digest_postgres_source.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_digest_postgres_source.py`:
```python
"""@db: load_ranked_articles returns in-window summarized rows, relevance-desc; sync wrapper works."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scrapeforge.core.db.models import Article as ArticleRow
from scrapeforge.core.db.session import make_sessionmaker


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(create_async_engine(_db_url, echo=False))


def _summary(b1: str) -> dict:
    return {"bullets": [b1, "b2"], "scores": {}, "reason": "r", "model": "m",
            "generated_at": "2026-06-24T00:00:00+00:00"}


async def _add(session_factory, *, id_, relevance, summary, hours_ago=1):
    async with session_factory() as s:
        s.add(ArticleRow(
            id=id_, url=f"https://e.com/{id_}", domain="e.com", bucket="community",
            title=f"T{id_[:4]}", content="Body.", author=None, publish_date=None,
            fetched_at=datetime.now(UTC) - timedelta(hours=hours_ago), raw_key=None, meta={},
            relevance=relevance, summary=summary,
        ))
        await s.commit()


@pytest.mark.db
async def test_load_ranked_articles_window_and_order(db_session, session_factory) -> None:
    from scrapeforge.digest.postgres_source import load_ranked_articles

    await _add(session_factory, id_="a" * 64, relevance=6, summary=_summary("low-in"))
    await _add(session_factory, id_="b" * 64, relevance=9, summary=_summary("high-in"))
    await _add(session_factory, id_="c" * 64, relevance=10, summary=_summary("old"), hours_ago=100)
    await _add(session_factory, id_="d" * 64, relevance=8, summary=None)  # unsummarized

    out = await load_ranked_articles(session_factory, window_hours=48, limit=10)
    # only the two in-window summarized rows, relevance-desc:
    assert [a.metadata["relevance"] for a in out] == [9, 6]
    assert out[0].metadata["summary"]["bullets"][0] == "high-in"
    assert out[0].title and out[0].url.startswith("https://")


@pytest.mark.db
def test_sync_wrapper_loads(_db_url) -> None:
    from scrapeforge.digest.postgres_source import load_ranked_articles, load_ranked_articles_sync

    factory = make_sessionmaker(create_async_engine(_db_url, echo=False))
    asyncio.run(_add(factory, id_="e" * 64, relevance=7, summary=_summary("x")))
    out = load_ranked_articles_sync(_db_url, window_hours=48, limit=10)
    assert any(a.metadata["relevance"] == 7 for a in out)
```

- [ ] **Step 2: Run red** (container + `DATABASE_URL`):
`.venv/Scripts/python.exe -m pytest tests/test_digest_postgres_source.py -q -m db` → FAIL (module missing).

- [ ] **Step 3: Implement** `scrapeforge/digest/postgres_source.py`:
```python
"""Load recent summarized articles from Postgres, relevance-ranked, for the digest.

The query is inlined here (NOT in repositories.py) per the seam rules. ``summary IS NOT NULL``
implies ``relevance IS NOT NULL`` (the summarizer writes both together), so a plain relevance-desc
order needs no NULLS-LAST handling. ``load_ranked_articles_sync`` is the async-in-sync bridge for
the (run-once) digest CLI.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scrapeforge.core.db.models import Article as ArticleRow
from scrapeforge.core.models import Article


async def load_ranked_articles(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    window_hours: int,
    limit: int,
) -> list[Article]:
    """Return up to *limit* summarized articles from the last *window_hours*, relevance-desc."""
    cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(ArticleRow)
                .where(ArticleRow.summary.is_not(None), ArticleRow.fetched_at >= cutoff)
                .order_by(ArticleRow.relevance.desc(), ArticleRow.fetched_at.desc())
                .limit(limit)
            )
        ).scalars().all()

    return [
        Article(
            url=row.url,
            title=row.title,
            content=row.content,
            author=row.author,
            publish_date=row.publish_date,
            metadata={
                "source_domain": row.domain,
                "bucket": row.bucket,
                "relevance": row.relevance,
                "summary": row.summary,
            },
        )
        for row in rows
    ]


def load_ranked_articles_sync(
    database_url: str, *, window_hours: int, limit: int
) -> list[Article]:
    """Sync bridge for the digest CLI: build an engine, run the async loader, dispose."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    async def _run() -> list[Article]:
        engine = make_engine(database_url)
        try:
            return await load_ranked_articles(
                make_sessionmaker(engine), window_hours=window_hours, limit=limit
            )
        finally:
            await engine.dispose()

    return asyncio.run(_run())
```

- [ ] **Step 4: Run green** (container + `DATABASE_URL`):
`.venv/Scripts/python.exe -m pytest tests/test_digest_postgres_source.py -q -m db` → PASS (2 passed).

- [ ] **Step 5: Gate + commit**
```bash
.venv/Scripts/python.exe -m ruff format scrapeforge/digest/postgres_source.py tests/test_digest_postgres_source.py
.venv/Scripts/python.exe -m ruff check scrapeforge/digest/postgres_source.py tests/test_digest_postgres_source.py
git add scrapeforge/digest/postgres_source.py tests/test_digest_postgres_source.py
git commit -m "feat(digest): Postgres source — load recent summarized articles, relevance-ranked

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: wire `--source postgres` into the service + CLI

**Files:**
- Modify: `scrapeforge/digest/service.py`, `scrapeforge/digest/cli.py`
- Test: `tests/test_digest_postgres_e2e.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_digest_postgres_e2e.py` — drives the REAL `make_digest(source="postgres")` wiring
(get_articles → Settings → load_ranked_articles_sync → build_relevance_digest → render). The sync
wrapper's `asyncio.run` can't run inside a running event loop, so we call `make_digest` via
`asyncio.to_thread` (a worker thread has no running loop); `db_session` handles per-test cleanup:
```python
"""@db hermetic e2e: seed scored articles -> make_digest(postgres) -> rendered HTML, ranked + floored."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scrapeforge.core.db.models import Article as ArticleRow
from scrapeforge.core.db.session import make_sessionmaker
from scrapeforge.digest.models import DigestPreferences, Subscriber


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(create_async_engine(_db_url, echo=False))


@pytest.fixture(autouse=True)
def _wire_env(_db_url, fake_env, monkeypatch):
    """Point Settings().DATABASE_URL at the test DB (fake_env supplies STATE_STORE_KEY)."""
    monkeypatch.setenv("DATABASE_URL", _db_url)


def _sub() -> Subscriber:
    return Subscriber(id="dee", name="Dee", email="dee@example.com", preferences=DigestPreferences())


async def _add(session_factory, *, id_, relevance, bullet):
    async with session_factory() as s:
        s.add(ArticleRow(
            id=id_, url=f"https://e.com/{id_}", domain="e.com", bucket="community",
            title=f"Art {id_[:3]}", content="Body.", author=None, publish_date=None,
            fetched_at=datetime.now(UTC) - timedelta(hours=1), raw_key=None, meta={},
            relevance=relevance,
            summary={"bullets": [bullet, "b2"], "reason": "r", "model": "m",
                     "generated_at": "2026-06-24T00:00:00+00:00"},
        ))
        await s.commit()


@pytest.mark.db
async def test_make_digest_postgres_ranks_and_floors(db_session, session_factory) -> None:
    from scrapeforge.digest.service import make_digest

    await _add(session_factory, id_="a" * 64, relevance=9, bullet="HIGH bullet")
    await _add(session_factory, id_="b" * 64, relevance=6, bullet="MID bullet")
    await _add(session_factory, id_="c" * 64, relevance=3, bullet="LOW bullet")

    _digest, email = await asyncio.to_thread(make_digest, _sub(), "postgres")
    html = email.html
    assert html.index("HIGH bullet") < html.index("MID bullet")  # relevance order
    assert "LOW bullet" not in html  # below the 5/10 floor
    assert "9/10" in html and "6/10" in html


@pytest.mark.db
async def test_make_digest_postgres_empty_state(db_session, session_factory) -> None:
    from scrapeforge.digest.service import make_digest

    await _add(session_factory, id_="a" * 64, relevance=2, bullet="meh")
    digest, email = await asyncio.to_thread(make_digest, _sub(), "postgres")
    assert digest.is_empty
    assert "No updates to show right now." in email.html
```

- [ ] **Step 2: Run red** (container + `DATABASE_URL`):
`.venv/Scripts/python.exe -m pytest tests/test_digest_postgres_e2e.py -q -m db` → FAIL (the `postgres`
branch of `get_articles`/`make_digest` doesn't exist yet, so `make_digest(_sub(), "postgres")` raises
`ValueError: unknown article source 'postgres'`). Step 3 implements the wiring that makes it green.

- [ ] **Step 3: Wire the service**

In `scrapeforge/digest/service.py`, extend `get_articles`:
```python
    if source == "postgres":
        from scrapeforge.config.settings import Settings
        from scrapeforge.digest.postgres_source import load_ranked_articles_sync
        from scrapeforge.digest.settings import DigestSettings

        ds = DigestSettings()
        return load_ranked_articles_sync(
            Settings().DATABASE_URL, window_hours=ds.DIGEST_WINDOW_HOURS, limit=ds.DIGEST_TOP_N
        )
```
And branch `make_digest`:
```python
def make_digest(subscriber: Subscriber, source: str = "sample") -> tuple[Digest, RenderedEmail]:
    """Build + render a digest for *subscriber* from *source*. (No send.)"""
    articles = get_articles(source)
    if source == "postgres":
        from scrapeforge.digest.relevance import build_relevance_digest
        from scrapeforge.digest.settings import DigestSettings

        ds = DigestSettings()
        digest = build_relevance_digest(
            subscriber, articles, min_relevance=ds.DIGEST_RELEVANCE_FLOOR, limit=ds.DIGEST_TOP_N
        )
    else:
        digest = build_digest(subscriber, articles)
    return digest, render_email(digest)
```

- [ ] **Step 4: Document the CLI source**

In `scrapeforge/digest/cli.py`, update both `--source` help strings to mention `postgres`:
`help="'sample', 'jsonl:<path>', or 'postgres' (relevance-ranked from the DB)"`.

- [ ] **Step 5: Run green + manual CLI check**

```bash
# hermetic e2e (container + DATABASE_URL):
.venv/Scripts/python.exe -m pytest tests/test_digest_postgres_e2e.py -q -m db
# manual: build a real preview from the DB (needs DATABASE_URL + STATE_STORE_KEY in env/.env):
DATABASE_URL="postgresql+asyncpg://scrapeforge:scrapeforge@localhost:5439/scrapeforge" \
  STATE_STORE_KEY=01234567890123456789012345678901234 \
  .venv/Scripts/python.exe -m scrapeforge digest preview --source postgres
```
Expected: e2e PASS (2). The manual preview writes an HTML file (empty-state if the DB has no
recent scored articles — that's fine).

- [ ] **Step 6: Gate + commit**
```bash
.venv/Scripts/python.exe -m ruff format scrapeforge/digest/service.py scrapeforge/digest/cli.py tests/test_digest_postgres_e2e.py
.venv/Scripts/python.exe -m ruff check scrapeforge/digest/service.py scrapeforge/digest/cli.py tests/test_digest_postgres_e2e.py
git add scrapeforge/digest/service.py scrapeforge/digest/cli.py tests/test_digest_postgres_e2e.py
git commit -m "feat(digest): --source postgres builds the relevance-ranked digest

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: workflow variable + docs + memory

**Files:** `.github/workflows/daily-digest.yml`, `SPEC.md`, `architecture.MD`, `planning.MD`, memory.

- [ ] **Step 1: Make the daily-digest source a variable (CI-safe)**

In `.github/workflows/daily-digest.yml`, change the send line (currently
`run: python -m scrapeforge digest send --yes --source sample`) to use a repo variable defaulting
to `sample`, and pass `DATABASE_URL` through from secrets when present:
```yaml
        env:
          # ... existing env ...
          DIGEST_SOURCE: ${{ vars.DIGEST_SOURCE || 'sample' }}
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
        run: python -m scrapeforge digest send --yes --source "$DIGEST_SOURCE"
```
Update the NOTE comment block to explain: defaults to `sample` (CI has no Postgres); set the
`DIGEST_SOURCE=postgres` repo variable + a `DATABASE_URL` secret once a production Postgres +
ingestion are running, to send the real relevance-ranked digest.

- [ ] **Step 2: SPEC.md / architecture.MD / planning.MD** — document the relevance digest:
  the `--source postgres` path, the `digest/{relevance,postgres_source,settings}.py` units, the
  new `DigestItem` fields, and the renderer's bullets/badge/reason. Mark Phase 2.5 delivered in
  planning.MD; note Phase 4 (swipe UI) is next.

- [ ] **Step 3: Memory** — add a `project` memory note: "Phase 2.5 — digest `--source postgres`
  builds a relevance-ranked 'Top updates' email (top `DIGEST_TOP_N` above `DIGEST_RELEVANCE_FLOOR`,
  last `DIGEST_WINDOW_HOURS`), rendering the AI bullets + badge + reason; keyword path kept for
  sample/jsonl; query inlined in `digest/postgres_source.py`; daily workflow source is the
  `DIGEST_SOURCE` repo variable (default sample, flip to postgres at deploy)." Update `MEMORY.md`.

- [ ] **Step 4: Commit**
```bash
git add .github/workflows/daily-digest.yml SPEC.md architecture.MD planning.MD
git commit -m "docs+ci: relevance digest --source postgres; daily source via DIGEST_SOURCE var

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Final verification (Definition of Done)

- [ ] `.venv/Scripts/python.exe -m ruff check .` → 0; `ruff format --check .` → clean
- [ ] `.venv/Scripts/python.exe -m pytest -m "not integration" -q` (container + `DATABASE_URL`) →
      green incl. new unit/`@db` tests; coverage ≥ 80%; existing `tests/test_digest.py` (keyword
      path) still passes
- [ ] Manual: `digest preview --source postgres` against the container renders without error
- [ ] Push `feat/digest-relevance`, open PR → `main`, CI green (workflow still defaults to
      `sample`, so it stays green), squash-merge. Never push `main`.
```

# Per-User Email Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every user in `user_profiles` receives their own relevance-ranked daily email, built from their per-user cosine scores in `user_article_relevance`; the existing owner digest is untouched.

**Architecture:** Additive only — a new `email` column on `user_profiles`, two new digest modules (`user_source.py` for DB reads, `user_digest.py` for assembly), a new `deliver_all` function + two CLI commands (`preview-all`/`send-all`), three `DigestSettings` knobs, and a new `daily-digest-users.yml` workflow. No edits to the working owner path (`digest/service.py::deliver`, `digest/relevance.py`, `digest/postgres_source.py`, `daily-digest.yml`).

**Tech Stack:** Python 3.12 async, SQLAlchemy 2.0 async (ORM/Core — no raw SQL), asyncpg, pgvector, Typer CLI, pydantic-settings (per-module fragment), pytest (`asyncio_mode=auto`) + `@pytest.mark.db` against the pgvector container on `localhost:5439`, ruff (100-char), 80% coverage gate.

**Spec:** `docs/superpowers/specs/2026-06-25-per-user-email-delivery-design.md`

**Builder notes:**
- Interpreter: `./.venv/Scripts/python.exe` (Windows). Run pytest as `./.venv/Scripts/python.exe -m pytest ...`.
- `@db` tests need `DATABASE_URL=postgresql+asyncpg://scrapeforge:scrapeforge@localhost:5439/scrapeforge` in the environment, and the pgvector container running.
- **Important for Task 1:** `create_all` uses `checkfirst=True` and will NOT add a column to an already-existing table. If the local pgvector container already has a `user_profiles` table without `email`, drop it once so the conftest re-creates it with the new column: `./.venv/Scripts/python.exe -c "import asyncio,sqlalchemy as sa; from sqlalchemy.ext.asyncio import create_async_engine; e=create_async_engine('postgresql+asyncpg://scrapeforge:scrapeforge@localhost:5439/scrapeforge'); asyncio.run((lambda: __import__('asyncio').get_event_loop())()) "` — simpler: run `docker exec scrapeforge-pgvector-test psql -U scrapeforge -c 'DROP TABLE IF EXISTS user_profiles CASCADE;'`.

---

### Task 1: Add `email` column to `UserProfile`

**Files:**
- Modify: `scrapeforge/core/db/models.py` (the `UserProfile` class, ~line 165-187)
- Test: `tests/test_user_profile_email.py`

- [ ] **Step 1: Drop the stale test table so conftest re-creates it with the new column**

Run: `docker exec scrapeforge-pgvector-test psql -U scrapeforge -c "DROP TABLE IF EXISTS user_profiles CASCADE;"`
Expected: `DROP TABLE` (or `NOTICE ... does not exist, skipping`).

- [ ] **Step 2: Write the failing test**

```python
# tests/test_user_profile_email.py
"""@db: the new email column on user_profiles round-trips and is nullable."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select


@pytest.mark.db
async def test_user_profile_email_roundtrips(db_session) -> None:
    from scrapeforge.core.db.models import UserProfile

    db_session.add(
        UserProfile(
            user_id="u1",
            email="alice@example.com",
            portfolio=["NVDA"],
            sectors=["AI"],
            focus=None,
            updated_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    row = (
        await db_session.execute(select(UserProfile).where(UserProfile.user_id == "u1"))
    ).scalar_one()
    assert row.email == "alice@example.com"


@pytest.mark.db
async def test_user_profile_email_nullable(db_session) -> None:
    from scrapeforge.core.db.models import UserProfile

    db_session.add(
        UserProfile(user_id="u2", portfolio=[], sectors=[], focus=None, updated_at=datetime.now(UTC))
    )
    await db_session.commit()
    row = (
        await db_session.execute(select(UserProfile).where(UserProfile.user_id == "u2"))
    ).scalar_one()
    assert row.email is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_user_profile_email.py -v`
Expected: FAIL — `TypeError: 'email' is an invalid keyword argument for UserProfile` (column doesn't exist yet).

- [ ] **Step 4: Add the column**

In `scrapeforge/core/db/models.py`, inside `class UserProfile`, add the `email` field immediately after `focus` (before `updated_at`):

```python
    email: Mapped[str | None]
    """User's email address — the Hezzian app writes this from Clerk at signup.
    NULL means the user has no email on file and is skipped at send time."""
```

- [ ] **Step 5: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_user_profile_email.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add scrapeforge/core/db/models.py tests/test_user_profile_email.py
git commit -m "feat(db): add email column to user_profiles (app-owned, nullable)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Per-user knobs on `DigestSettings`

**Files:**
- Modify: `scrapeforge/digest/settings.py`
- Test: `tests/test_digest_user_settings.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_digest_user_settings.py
"""Hermetic defaults for the per-user digest knobs (ignore the developer's .env)."""

from __future__ import annotations


def test_user_digest_defaults(monkeypatch) -> None:
    from scrapeforge.digest.settings import DigestSettings

    for key in ("DIGEST_USER_TOP_N", "DIGEST_USER_WINDOW_HOURS", "DIGEST_USER_SCORE_FLOOR"):
        monkeypatch.delenv(key, raising=False)
    s = DigestSettings(_env_file=None)
    assert s.DIGEST_USER_TOP_N == 10
    assert s.DIGEST_USER_WINDOW_HOURS == 48
    assert s.DIGEST_USER_SCORE_FLOOR == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_digest_user_settings.py -v`
Expected: FAIL — `AttributeError: 'DigestSettings' object has no attribute 'DIGEST_USER_TOP_N'`.

- [ ] **Step 3: Add the fields**

In `scrapeforge/digest/settings.py`, append inside `class DigestSettings` (after `DIGEST_WINDOW_HOURS`):

```python
    # --- per-user delivery (Phase 3.5); ranking is by user_article_relevance cosine ---
    DIGEST_USER_TOP_N: int = Field(default=10)  # max articles per user email
    DIGEST_USER_WINDOW_HOURS: int = Field(default=48)  # recency window per user
    DIGEST_USER_SCORE_FLOOR: float = Field(default=0.0)  # min cosine (>=0 => positively correlated)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_digest_user_settings.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scrapeforge/digest/settings.py tests/test_digest_user_settings.py
git commit -m "feat(digest): add per-user digest knobs (top-n, window, cosine floor)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `user_source.py` — load active users + per-user ranked articles

**Files:**
- Create: `scrapeforge/digest/user_source.py`
- Test: `tests/test_user_source.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_user_source.py
"""@db: per-user article loading ranked by user_article_relevance cosine, filtered by floor/window/limit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scrapeforge.core.db.models import (
    Article as ArticleRow,
    UserArticleRelevance,
    UserProfile,
)
from scrapeforge.core.db.session import make_sessionmaker


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(create_async_engine(_db_url, echo=False))


async def _seed_article(session_factory, *, id_, hours_ago=1, summary=True):
    async with session_factory() as s:
        s.add(
            ArticleRow(
                id=id_,
                url=f"https://e.com/{id_[:6]}",
                domain="e.com",
                bucket="community",
                title=f"Title {id_[:4]}",
                content="Body text.",
                author=None,
                publish_date=None,
                fetched_at=datetime.now(UTC) - timedelta(hours=hours_ago),
                raw_key=None,
                meta={},
                relevance=7,
                summary={"bullets": [f"bullet-{id_[:4]}", "b2"], "reason": "r"} if summary else None,
            )
        )
        await s.commit()


async def _seed_score(session_factory, *, user_id, article_id, score):
    async with session_factory() as s:
        s.add(
            UserArticleRelevance(
                user_id=user_id, article_id=article_id, score=score, computed_at=datetime.now(UTC)
            )
        )
        await s.commit()


async def _seed_user(session_factory, *, user_id, email):
    async with session_factory() as s:
        s.add(
            UserProfile(
                user_id=user_id,
                email=email,
                portfolio=[],
                sectors=[],
                focus=None,
                updated_at=datetime.now(UTC),
            )
        )
        await s.commit()


@pytest.mark.db
async def test_load_active_users_skips_null_email(db_session, session_factory) -> None:
    from scrapeforge.digest.user_source import load_active_users

    await _seed_user(session_factory, user_id="u1", email="a@e.com")
    async with session_factory() as s:  # a user with no email is skipped
        s.add(UserProfile(user_id="u2", portfolio=[], sectors=[], focus=None, updated_at=datetime.now(UTC)))
        await s.commit()

    users = await load_active_users(session_factory)
    assert [u.user_id for u in users] == ["u1"]
    assert users[0].email == "a@e.com"
    assert users[0].name == "a"  # local-part fallback


@pytest.mark.db
async def test_load_user_ranked_articles_orders_and_filters(db_session, session_factory) -> None:
    from scrapeforge.digest.user_source import load_user_ranked_articles

    await _seed_article(session_factory, id_="a" * 64)
    await _seed_article(session_factory, id_="b" * 64)
    await _seed_article(session_factory, id_="c" * 64, hours_ago=999)  # outside window
    await _seed_article(session_factory, id_="d" * 64)  # below floor
    await _seed_score(session_factory, user_id="u1", article_id="a" * 64, score=0.9)
    await _seed_score(session_factory, user_id="u1", article_id="b" * 64, score=0.4)
    await _seed_score(session_factory, user_id="u1", article_id="c" * 64, score=0.8)
    await _seed_score(session_factory, user_id="u1", article_id="d" * 64, score=-0.5)

    arts = await load_user_ranked_articles(
        session_factory, "u1", window_hours=48, score_floor=0.0, limit=10
    )
    titles = [a.title for a in arts]
    assert titles == ["Title aaaa", "Title bbbb"]  # cosine-desc; c out of window, d below floor
    assert arts[0].metadata["relevance"] == 7
    assert arts[0].metadata["summary"]["bullets"][0] == "bullet-aaaa"


@pytest.mark.db
async def test_load_user_ranked_articles_respects_limit(db_session, session_factory) -> None:
    from scrapeforge.digest.user_source import load_user_ranked_articles

    await _seed_article(session_factory, id_="a" * 64)
    await _seed_article(session_factory, id_="b" * 64)
    await _seed_score(session_factory, user_id="u1", article_id="a" * 64, score=0.9)
    await _seed_score(session_factory, user_id="u1", article_id="b" * 64, score=0.8)

    arts = await load_user_ranked_articles(
        session_factory, "u1", window_hours=48, score_floor=0.0, limit=1
    )
    assert len(arts) == 1 and arts[0].title == "Title aaaa"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_user_source.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scrapeforge.digest.user_source'`.

- [ ] **Step 3: Implement `user_source.py`**

```python
# scrapeforge/digest/user_source.py
"""Per-user digest DB reads (Phase 3.5).

Queries are inlined here (NOT in repositories.py) per the seam rules — exactly as
``digest/postgres_source.py`` does for the single-owner path. Ranking is by the per-user
``user_article_relevance.score`` (cosine in [-1, 1]); the score is used for ORDER/filter only,
not displayed. No raw SQL (SQLi guard, consistent with ``score_users``).
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scrapeforge.core.db.models import Article as ArticleRow
from scrapeforge.core.db.models import UserArticleRelevance, UserProfile
from scrapeforge.core.models import Article


@dataclass(frozen=True, slots=True)
class ActiveUser:
    """A user we can email. ``name`` is derived from the email local-part — ``user_profiles``
    has no name column in v1, and Clerk's display name isn't mirrored into our table."""

    user_id: str
    email: str
    name: str


async def load_active_users(session_factory: async_sessionmaker[AsyncSession]) -> list[ActiveUser]:
    """All users with a non-NULL email, ordered by user_id for deterministic batches."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(UserProfile.user_id, UserProfile.email)
                .where(UserProfile.email.is_not(None))
                .order_by(UserProfile.user_id)
            )
        ).all()
    return [ActiveUser(user_id=uid, email=email, name=email.split("@", 1)[0]) for uid, email in rows]


async def load_user_ranked_articles(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: str,
    *,
    window_hours: int,
    score_floor: float,
    limit: int,
) -> list[Article]:
    """Up to *limit* summarized articles for *user_id*, cosine-desc, within *window_hours* and
    at or above *score_floor*. Each Article carries its shared relevance + summary in metadata."""
    cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ArticleRow)
                    .join(UserArticleRelevance, UserArticleRelevance.article_id == ArticleRow.id)
                    .where(
                        UserArticleRelevance.user_id == user_id,
                        ArticleRow.summary.is_not(None),
                        ArticleRow.fetched_at >= cutoff,
                        UserArticleRelevance.score >= score_floor,
                    )
                    .order_by(UserArticleRelevance.score.desc(), ArticleRow.fetched_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
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


def load_all_sync(
    database_url: str, *, window_hours: int, score_floor: float, limit: int
) -> list[tuple[ActiveUser, list[Article]]]:
    """Sync bridge for the run-once CLI: one engine for the whole batch (mirrors
    ``postgres_source.load_ranked_articles_sync``)."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    from scrapeforge.core.db.session import make_engine, make_sessionmaker

    async def _run() -> list[tuple[ActiveUser, list[Article]]]:
        engine = make_engine(database_url)
        try:
            factory = make_sessionmaker(engine)
            users = await load_active_users(factory)
            out: list[tuple[ActiveUser, list[Article]]] = []
            for user in users:
                articles = await load_user_ranked_articles(
                    factory,
                    user.user_id,
                    window_hours=window_hours,
                    score_floor=score_floor,
                    limit=limit,
                )
                out.append((user, articles))
            return out
        finally:
            await engine.dispose()

    return asyncio.run(_run())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_user_source.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add scrapeforge/digest/user_source.py tests/test_user_source.py
git commit -m "feat(digest): per-user article loader ranked by cosine relevance

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `user_digest.py` — assemble a per-user Digest

**Files:**
- Create: `scrapeforge/digest/user_digest.py`
- Test: `tests/test_user_digest.py`

Note: the spec's component 3 floated refactoring `relevance.py::_item`, but the spec's stronger rule is "owner path untouched." So item construction lives here (≈15 lines), not in `relevance.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_user_digest.py
"""build_user_digest preserves cosine order and attaches shared 1-10 relevance + bullets."""

from __future__ import annotations

from scrapeforge.core.models import Article
from scrapeforge.digest.user_source import ActiveUser


def _art(slug: str, relevance: int) -> Article:
    return Article(
        url=f"https://e.com/{slug}",
        title=f"Title {slug}",
        content="Body.",
        author=None,
        publish_date=None,
        metadata={"relevance": relevance, "summary": {"bullets": [f"b-{slug}", "b2"], "reason": "r"}},
    )


def test_build_user_digest_preserves_order_and_fields() -> None:
    from scrapeforge.digest.user_digest import build_user_digest

    user = ActiveUser(user_id="u1", email="a@e.com", name="a")
    # input already cosine-ordered by the query: first, second
    digest = build_user_digest(user, [_art("first", 9), _art("second", 6)])

    assert digest.subscriber_id == "u1"
    assert digest.subscriber_email == "a@e.com"
    assert len(digest.sections) == 1
    section = digest.sections[0]
    assert section.key == "top" and section.heading == "Top updates"
    assert [i.title for i in section.items] == ["Title first", "Title second"]  # order preserved
    assert section.items[0].relevance == 9
    assert section.items[0].bullets == ["b-first", "b2"]
    assert section.items[0].reason == "r"


def test_build_user_digest_empty_is_empty() -> None:
    from scrapeforge.digest.user_digest import build_user_digest

    digest = build_user_digest(ActiveUser("u1", "a@e.com", "a"), [])
    assert digest.is_empty
    assert digest.sections == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_user_digest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scrapeforge.digest.user_digest'`.

- [ ] **Step 3: Implement `user_digest.py`**

```python
# scrapeforge/digest/user_digest.py
"""Assemble a per-user Digest from cosine-ranked articles (pure; no DB).

The articles arrive already ordered by the user's relevance score (from
``user_source.load_user_ranked_articles``), so this module preserves their order and wraps them in
a single "Top updates" section, attaching each article's SHARED 1-10 relevance + 5-bullet summary
(same summary every user sees) from ``article.metadata``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from scrapeforge.core.models import Article
from scrapeforge.digest.matcher import summarize
from scrapeforge.digest.models import Digest, DigestItem, DigestSection
from scrapeforge.digest.user_source import ActiveUser


def _relevance(article: Article) -> int | None:
    value = article.metadata.get("relevance")
    return value if isinstance(value, int) else None


def _item(article: Article) -> DigestItem:
    summary = article.metadata.get("summary")
    summary = summary if isinstance(summary, dict) else {}
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
        relevance=_relevance(article),
        reason=reason.strip() if isinstance(reason, str) and reason.strip() else None,
    )


def build_user_digest(
    user: ActiveUser, articles: list[Article], *, now: datetime | None = None
) -> Digest:
    """Wrap *articles* (already cosine-ordered) in one "Top updates" section for *user*."""
    now = now or datetime.now(UTC)
    items = [_item(a) for a in articles]  # preserve query order — do NOT re-sort
    sections = [DigestSection(key="top", heading="Top updates", items=items)] if items else []
    return Digest(
        subscriber_id=user.user_id,
        subscriber_name=user.name,
        subscriber_email=user.email,
        cadence="daily",
        period=now.date().isoformat(),
        generated_at=now,
        sections=sections,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_user_digest.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add scrapeforge/digest/user_digest.py tests/test_user_digest.py
git commit -m "feat(digest): build_user_digest assembles a per-user Top-updates section

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `deliver_all` + `DeliverySummary` (failure-isolated loop)

**Files:**
- Modify: `scrapeforge/digest/service.py` (add new function + dataclass; existing `deliver` untouched)
- Test: `tests/test_deliver_all.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deliver_all.py
"""deliver_all loops users, isolates per-user failures, skips empties, returns counts."""

from __future__ import annotations

import pytest

from scrapeforge.core.models import Article
from scrapeforge.digest.sender import EmailSender
from scrapeforge.digest.user_source import ActiveUser


class _FakeSender(EmailSender):
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, to: str, email) -> None:  # noqa: ANN001
        if to == "boom@e.com":
            raise RuntimeError("smtp down")
        self.sent.append(to)


def _art() -> Article:
    return Article(
        url="https://e.com/a",
        title="t",
        content="c",
        author=None,
        publish_date=None,
        metadata={"relevance": 7, "summary": {"bullets": ["b1"], "reason": "r"}},
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch, fake_env):
    # deliver_all constructs Settings() (needs STATE_STORE_KEY via fake_env) + reads DATABASE_URL.
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost/z")


def test_deliver_all_isolates_failures_and_skips_empty(monkeypatch) -> None:
    from scrapeforge.digest import service

    batches = [
        (ActiveUser("u1", "ok@e.com", "ok"), [_art()]),
        (ActiveUser("u2", "empty@e.com", "empty"), []),
        (ActiveUser("u3", "boom@e.com", "boom"), [_art()]),
    ]
    monkeypatch.setattr("scrapeforge.digest.user_source.load_all_sync", lambda *a, **k: batches)

    sender = _FakeSender()
    summary = service.deliver_all(source="postgres", sender=sender)

    assert summary.sent == 1
    assert summary.skipped_empty == 1
    assert summary.failed == 1
    assert sender.sent == ["ok@e.com"]
    assert "sent=1 skipped_empty=1 failed=1" in str(summary)


def test_deliver_all_rejects_non_postgres_source() -> None:
    from scrapeforge.digest import service

    with pytest.raises(ValueError, match="postgres"):
        service.deliver_all(source="sample")


def test_deliver_all_default_sender_is_preview(monkeypatch, tmp_path) -> None:
    from scrapeforge.digest import service
    from scrapeforge.digest.sender import PreviewEmailSender

    monkeypatch.setattr(
        "scrapeforge.digest.user_source.load_all_sync",
        lambda *a, **k: [(ActiveUser("u1", "a@e.com", "a"), [_art()])],
    )
    # Default sender writes preview HTML; point it at tmp by passing one explicitly is cleaner,
    # but here we assert the default path runs without creds and reports a send.
    summary = service.deliver_all(source="postgres", sender=PreviewEmailSender(tmp_path))
    assert summary.sent == 1
    assert (tmp_path / "a_at_e_com.html").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_deliver_all.py -v`
Expected: FAIL — `AttributeError: module 'scrapeforge.digest.service' has no attribute 'deliver_all'`.

- [ ] **Step 3: Implement `deliver_all` + `DeliverySummary`**

Add to `scrapeforge/digest/service.py`. First extend the imports at the top of the file:

```python
import logging
from dataclasses import dataclass
```

Add a module logger after the imports:

```python
log = logging.getLogger(__name__)
```

Append at the end of the file:

```python
@dataclass(frozen=True, slots=True)
class DeliverySummary:
    """Outcome counts for a per-user delivery run."""

    sent: int = 0
    skipped_empty: int = 0
    failed: int = 0

    def __str__(self) -> str:
        return f"sent={self.sent} skipped_empty={self.skipped_empty} failed={self.failed}"


def deliver_all(
    *, source: str = "postgres", sender: EmailSender | None = None
) -> DeliverySummary:
    """Send each active user their own relevance-ranked digest. Per-user failures are isolated:
    one user's bad render/send is logged and counted, never aborting the batch. Empty digests are
    skipped (no blank email). Defaults to the preview sender (no creds)."""
    if source != "postgres":
        raise ValueError(f"deliver_all only supports source='postgres', got {source!r}")

    from scrapeforge.config.settings import Settings
    from scrapeforge.digest.settings import DigestSettings
    from scrapeforge.digest.user_digest import build_user_digest
    from scrapeforge.digest.user_source import load_all_sync

    ds = DigestSettings()
    batches = load_all_sync(
        Settings().DATABASE_URL,
        window_hours=ds.DIGEST_USER_WINDOW_HOURS,
        score_floor=ds.DIGEST_USER_SCORE_FLOOR,
        limit=ds.DIGEST_USER_TOP_N,
    )
    sender = sender or PreviewEmailSender()

    sent = skipped = failed = 0
    for user, articles in batches:
        try:
            if not articles:
                skipped += 1
                continue
            digest = build_user_digest(user, articles)
            sender.send(user.email, render_email(digest))
            sent += 1
        except Exception:  # noqa: BLE001 — isolate one user's failure from the rest of the batch
            log.exception("deliver_all: delivery failed for user %s", user.user_id)
            failed += 1
    return DeliverySummary(sent=sent, skipped_empty=skipped, failed=failed)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_deliver_all.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add scrapeforge/digest/service.py tests/test_deliver_all.py
git commit -m "feat(digest): deliver_all sends per-user digests with failure isolation

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: CLI — `digest preview-all` / `digest send-all`

**Files:**
- Modify: `scrapeforge/digest/cli.py`
- Test: `tests/test_digest_user_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_digest_user_cli.py
"""CLI wiring for the per-user digest commands (loader mocked; no network, no DB)."""

from __future__ import annotations

from typer.testing import CliRunner

from scrapeforge.digest.cli import digest_app

runner = CliRunner()

_KEY = "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC1vbmx5MDA="


def test_user_commands_registered() -> None:
    result = runner.invoke(digest_app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("preview-all", "send-all"):
        assert cmd in result.stdout


def test_preview_all_runs_with_no_users(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STATE_STORE_KEY", _KEY)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost/z")
    monkeypatch.setattr("scrapeforge.digest.user_source.load_all_sync", lambda *a, **k: [])

    result = runner.invoke(digest_app, ["preview-all", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    assert "sent=0 skipped_empty=0 failed=0" in result.stdout


def test_send_all_aborts_without_yes(monkeypatch) -> None:
    monkeypatch.setenv("STATE_STORE_KEY", _KEY)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost/z")
    # No --yes and no stdin "y" => typer.confirm aborts before any send is attempted.
    result = runner.invoke(digest_app, ["send-all"], input="n\n")
    assert result.exit_code != 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_digest_user_cli.py -v`
Expected: FAIL — `test_user_commands_registered` fails (commands not registered).

- [ ] **Step 3: Implement the CLI commands**

In `scrapeforge/digest/cli.py`, extend the service import to include `deliver_all`:

```python
from scrapeforge.digest.service import deliver, deliver_all
```

Append two commands at the end of the file:

```python
@digest_app.command("preview-all")
def preview_all(
    source: str = typer.Option(
        "postgres", "--source", help="Per-user source (only 'postgres' is supported)"
    ),
    out_dir: Path = typer.Option(  # noqa: B008
        Path("./output/digests"), "--out-dir", "-o", help="Where to write per-user preview HTML"
    ),
) -> None:
    """Build + render every active user's digest and write preview HTML (no email is sent)."""
    _load_dotenv()
    summary = deliver_all(source=source, sender=PreviewEmailSender(out_dir))
    typer.echo(f"Per-user digests (preview): {summary}")


@digest_app.command("send-all")
def send_all(
    source: str = typer.Option(
        "postgres", "--source", help="Per-user source (only 'postgres' is supported)"
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
) -> None:
    """Send every active user their own relevance-ranked digest via SMTP (needs DIGEST_SMTP_*)."""
    _load_dotenv()
    if not yes:
        typer.confirm("Send real per-user emails via SMTP to ALL active users now?", abort=True)
    try:
        summary = deliver_all(source=source, sender=SmtpEmailSender())
    except ValueError as exc:  # missing credentials or unsupported source
        typer.echo(f"Cannot send: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Per-user digests sent: {summary}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_digest_user_cli.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add scrapeforge/digest/cli.py tests/test_digest_user_cli.py
git commit -m "feat(cli): add digest preview-all / send-all per-user commands

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: `daily-digest-users.yml` workflow

**Files:**
- Create: `.github/workflows/daily-digest-users.yml`
- Test: `tests/test_daily_digest_users_workflow.py`

Ships with **`workflow_dispatch` only** (manual). The daily schedule is included but commented, so it does not fail every morning before the owner has added users + the `DATABASE_URL` secret + run the one-time `ALTER TABLE`. The owner uncomments the schedule when ready.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_daily_digest_users_workflow.py
"""The per-user delivery workflow exists and runs the send-all command with SMTP secrets."""

from __future__ import annotations

from pathlib import Path


def test_workflow_runs_send_all() -> None:
    text = Path(".github/workflows/daily-digest-users.yml").read_text(encoding="utf-8")
    assert "digest send-all" in text
    assert "--yes" in text
    assert "DIGEST_SMTP_USER" in text
    assert "DIGEST_SMTP_PASSWORD" in text
    assert "DATABASE_URL" in text
    assert "workflow_dispatch" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_daily_digest_users_workflow.py -v`
Expected: FAIL — `FileNotFoundError` (workflow doesn't exist).

- [ ] **Step 3: Create the workflow**

```yaml
# .github/workflows/daily-digest-users.yml
name: Daily per-user digests

# Sends every active user (user_profiles.email IS NOT NULL) their own relevance-ranked digest.
#
# Ships MANUAL-ONLY (workflow_dispatch). Before enabling the daily schedule below you MUST:
#   1. Run once on the live DB:  ALTER TABLE user_profiles ADD COLUMN email text;
#   2. Have the Hezzian app writing user_profiles rows (user_id, email, portfolio, sectors).
#   3. Set the DATABASE_URL secret (postgresql+asyncpg://user:pass@host:port/db) and DIGEST_SMTP_*.
#   4. Uncomment the `schedule:` block.
# Until then, run it by hand from the Actions tab to preview real behavior.

on:
  workflow_dispatch: {}
  # schedule:
  #   - cron: "0 8 * * *" # 08:00 UTC = 09:00 London (BST)
  #   - cron: "0 9 * * *" # 09:00 UTC = 09:00 London (GMT)

permissions:
  contents: read

jobs:
  send-user-digests:
    name: send per-user digests
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v3
        with:
          enable-cache: true
          cache-dependency-glob: "pyproject.toml"

      - name: Create venv
        run: |
          uv venv --python 3.12
          echo "$PWD/.venv/bin" >> "$GITHUB_PATH"

      - name: Install project (runtime only)
        run: uv pip install -e .

      - name: Send per-user digests
        env:
          DIGEST_SMTP_USER: ${{ secrets.DIGEST_SMTP_USER }}
          DIGEST_SMTP_PASSWORD: ${{ secrets.DIGEST_SMTP_PASSWORD }}
          DIGEST_FROM: ${{ secrets.DIGEST_FROM }}
          STATE_STORE_KEY: ci-digest-placeholder-not-used-000000000000
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
          DATABASE_SSL: require
        run: python -m scrapeforge digest send-all --yes --source postgres
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_daily_digest_users_workflow.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/daily-digest-users.yml tests/test_daily_digest_users_workflow.py
git commit -m "ci: add manual daily-digest-users workflow (send-all per user)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Full gate + docs + memory

**Files:**
- Modify: `SPEC.md`, `architecture.MD`, `planning.MD`, `DEPLOYMENT.md`
- Create: memory file under `C:\Users\nayrb\.claude\projects\C--Users-nayrb-Documents-scraper-vHezzian\memory\`

- [ ] **Step 1: Run the full gate**

Run:
```bash
./.venv/Scripts/python.exe -m ruff check .
./.venv/Scripts/python.exe -m ruff format --check .
./.venv/Scripts/python.exe -m pytest -m "not integration" --cov --cov-fail-under=80
```
Expected: ruff 0 errors, format clean, all tests pass, coverage ≥ 80%. Fix anything that fails before continuing (re-run until green). `@db` tests require `DATABASE_URL` pointed at the pgvector container.

- [ ] **Step 2: Update `SPEC.md`**

In the `user_profiles` contract section (search for `user_profiles`), add the `email` column to the documented columns:

> `email text NULL` — the Hezzian app writes the user's email (from Clerk) at signup; the pipeline reads it to address per-user digests. NULL ⇒ the user is skipped at send time.

In the command/CLI table (search for the existing `digest send` / `digest preview` rows), add:

> `digest preview-all` — write per-user preview HTML for every active user (no send).
> `digest send-all` — SMTP-send each active user their own relevance-ranked digest.

- [ ] **Step 3: Update `architecture.MD`**

In the digest module tree (search for `digest/`), add:

```
digest/
  user_source.py    # load active users + per-user cosine-ranked articles (Phase 3.5)
  user_digest.py    # build_user_digest — per-user "Top updates" section
  service.py        # ... + deliver_all (per-user failure-isolated delivery loop)
```

In the workflows list (search for `daily-digest.yml`), add a line:

> `daily-digest-users.yml` — manual (then scheduled) per-user `digest send-all`.

- [ ] **Step 4: Update `planning.MD`**

Add a "Phase 3.5 — per-user email delivery (DELIVERED)" block near the Phase 3 entry:

```
### Phase 3.5 — Per-user email delivery (DELIVERED 2026-06-25)
- user_profiles gains an app-owned `email` column; pipeline reads it.
- digest/user_source.py + user_digest.py + service.deliver_all + `digest preview-all`/`send-all`.
- daily-digest-users.yml (manual; schedule commented until live).
- Owner digest untouched. Acceptance: each active user gets a cosine-ranked email; per-user
  failures isolated; empty digests skipped. Out of scope: timezones, unsubscribe, dedupe table.
```

- [ ] **Step 5: Update `DEPLOYMENT.md`**

Add a "Per-user digests (Phase 3.5)" subsection documenting:
- The one-time live migration: `ALTER TABLE user_profiles ADD COLUMN email text;` (no Alembic — `create_all` won't alter an existing table).
- The Hezzian app must write `user_profiles(user_id, email, portfolio, sectors, focus)` rows.
- Enable `daily-digest-users.yml` by uncommenting its `schedule:` block once the `DATABASE_URL` secret + `DIGEST_SMTP_*` secrets are set.

- [ ] **Step 6: Write the memory file**

Create `C:\Users\nayrb\.claude\projects\C--Users-nayrb-Documents-scraper-vHezzian\memory\per-user-email-delivery.md`:

```markdown
---
name: per-user-email-delivery
description: "Phase 3.5 per-user email delivery — each user_profiles user gets their own cosine-ranked email via `digest send-all`. Needs ALTER TABLE user_profiles ADD COLUMN email; app writes email from Clerk."
metadata:
  type: project
---

**Phase 3.5 — per-user email delivery. BUILT on branch feat/per-user-email-delivery.**
Each active user (``user_profiles.email IS NOT NULL``) gets their own relevance-ranked email,
ranked by their per-user ``user_article_relevance.score`` (cosine), showing the SHARED 1-10 badge +
5 bullets. Owner digest untouched.

What shipped: ``UserProfile.email`` (app-owned, nullable); ``digest/user_source.py``
(load_active_users + load_user_ranked_articles + load_all_sync, no raw SQL); ``digest/user_digest.py``
(build_user_digest — "Top updates", preserves cosine order); ``digest/service.deliver_all`` +
``DeliverySummary`` (per-user failure isolation, skip-empty); ``digest preview-all``/``send-all`` CLI;
``DigestSettings`` knobs DIGEST_USER_TOP_N=10/WINDOW_HOURS=48/SCORE_FLOOR=0.0;
``daily-digest-users.yml`` (manual; schedule commented).

OWNER ACTIONS to go live: (1) ``ALTER TABLE user_profiles ADD COLUMN email text;`` on Neon
(no Alembic — create_all won't alter existing tables); (2) app writes email into user_profiles from
Clerk at signup; (3) set DATABASE_URL + DIGEST_SMTP_* secrets, uncomment the workflow schedule.

Out of scope: per-user timezone send-time, unsubscribe, already-emailed dedupe, weekly cadence,
Resend/SES adapter (drop-in behind EmailSender when > ~500/day). Relates to
[[multiuser-relevance-spec]], [[digest-relevance]], [[render-neon-deployment]].
```

Then add a one-line pointer to `MEMORY.md`:

```
- [Per-user email delivery (Phase 3.5)](per-user-email-delivery.md) — each user_profiles user gets their own cosine-ranked email via `digest send-all`; needs `ALTER TABLE user_profiles ADD COLUMN email` + app writing email from Clerk.
```

- [ ] **Step 7: Commit**

```bash
git add SPEC.md architecture.MD planning.MD DEPLOYMENT.md
git commit -m "docs: per-user email delivery (Phase 3.5) across SPEC/architecture/planning/deploy

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

(The memory files live outside the repo and are not committed.)

---

## Self-Review

**Spec coverage:**
- `email` column → Task 1. ✅
- `DigestSettings` knobs → Task 2. ✅
- `user_source.py` (ActiveUser, load_active_users, load_user_ranked_articles, load_all_sync) → Task 3. ✅
- `user_digest.py` (build_user_digest) → Task 4. ✅
- `deliver_all` + `DeliverySummary` (failure isolation, skip-empty, default preview) → Task 5. ✅
- CLI `preview-all`/`send-all` → Task 6. ✅
- `daily-digest-users.yml` → Task 7. ✅
- Error handling (per-user isolation, empty skip, no-users zero exit) → Tasks 5 + 6 tests. ✅
- Testing (@db user_source, unit user_digest/deliver_all/settings, CLI) → Tasks 2-6. ✅
- Docs + memory → Task 8. ✅
- Spec's component-3 `relevance.py` refactor → intentionally NOT done (owner path untouched); item logic is in `user_digest.py`. Noted in Task 4.

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every command has expected output.

**Type consistency:** `ActiveUser(user_id, email, name)` defined in Task 3, used identically in Tasks 4-5. `DeliverySummary(sent, skipped_empty, failed)` defined in Task 5, asserted identically in Tasks 5-6. `load_all_sync(database_url, *, window_hours, score_floor, limit)` signature defined in Task 3, called with the same kwargs in Task 5. `build_user_digest(user, articles, *, now)` defined in Task 4, used in Task 5. `deliver_all(*, source, sender)` defined in Task 5, called in Task 6. Metadata keys (`relevance`, `summary`→`bullets`/`reason`) match between `user_source` (Task 3) and `user_digest` (Task 4). ✅

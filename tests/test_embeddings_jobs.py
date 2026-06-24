"""@db: embed_articles / embed_profiles / score_users / seed_owner pipeline jobs."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scrapeforge.core.db.models import Article
from scrapeforge.core.embeddings.base import Embedder


class FakeEmbedder(Embedder):
    """Deterministic 1536-dim embedder: maps a marker substring to a unit axis."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out = []
        for t in texts:
            base = [0.0] * 1536
            if "AI" in t:
                base[0] = 1.0
            elif "OIL" in t:
                base[1] = 1.0
            else:
                base[2] = 1.0
            out.append(base)
        return out


@pytest.fixture
def session_factory(_db_url: str) -> async_sessionmaker:
    return async_sessionmaker(create_async_engine(_db_url, echo=False), expire_on_commit=False)


async def _add_article(session_factory, *, id_, title, content, fetched_at, embedding=None) -> None:
    async with session_factory() as s:
        s.add(
            Article(
                id=id_,
                url=f"https://e.com/{id_}",
                domain="e.com",
                bucket="community",
                title=title,
                content=content,
                fetched_at=fetched_at,
                meta={},
                embedding=embedding,
            )
        )
        await s.commit()


@pytest.mark.db
async def test_embed_articles_fills_null_only(db_session, session_factory) -> None:
    from scrapeforge.pipeline.embeddings_jobs import embed_articles

    now = datetime.now(UTC)
    await _add_article(session_factory, id_="a" * 64, title="AI chips", content="x", fetched_at=now)
    await _add_article(
        session_factory,
        id_="b" * 64,
        title="done",
        content="x",
        fetched_at=now,
        embedding=[0.5, 0.5, 0.5] + [0.0] * 1533,
    )

    n = await embed_articles(
        session_factory=session_factory, embedder=FakeEmbedder(), batch_size=10
    )
    assert n == 1  # only the NULL row embedded
    rows = (await db_session.execute(select(Article).order_by(Article.id))).scalars().all()
    assert list(rows[0].embedding)[0] == 1.0  # "AI" → x-axis, was NULL, now embedded
    assert list(rows[1].embedding)[:3] == [0.5, 0.5, 0.5]  # untouched


@pytest.mark.db
async def test_embed_articles_idempotent(db_session, session_factory) -> None:
    from scrapeforge.pipeline.embeddings_jobs import embed_articles

    now = datetime.now(UTC)
    await _add_article(session_factory, id_="a" * 64, title="AI", content="x", fetched_at=now)
    await embed_articles(session_factory=session_factory, embedder=FakeEmbedder(), batch_size=10)
    n2 = await embed_articles(
        session_factory=session_factory, embedder=FakeEmbedder(), batch_size=10
    )
    assert n2 == 0  # nothing left WHERE embedding IS NULL

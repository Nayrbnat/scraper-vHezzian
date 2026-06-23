"""Unit + contract tests for the curated investing-Substack source list (Bucket 2).

The list in ``scrapeforge.scrapers.community.substack_sources`` is a *data* artifact:
50 live-verified Substack publications focused on company/sector deep dives, plus the
selection helpers the community CLI uses to choose which ones to scrape.

These tests pin the invariants that keep the list useful:
- exactly 50 entries, all unique (no duplicate publication or display name);
- every base host is a plausible Substack host (custom domain or ``*.substack.com``);
- broad sector coverage (the user wants "all sectors", not just semis);
- the ``select_sources`` / ``by_sector`` / ``sectors`` helpers filter correctly.
"""

from __future__ import annotations

import re

import pytest

from scrapeforge.scrapers.community.substack_sources import (
    SUBSTACK_INVESTING_SOURCES,
    SubstackSource,
    by_sector,
    sectors,
    select_sources,
)

# A host is either a dotted custom domain (e.g. ``newsletter.semianalysis.com``)
# or a bare ``<slug>.substack.com`` — never a scheme, path, or query.
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


class TestCuratedList:
    def test_has_exactly_50_entries(self) -> None:
        assert len(SUBSTACK_INVESTING_SOURCES) == 50

    def test_all_entries_are_substack_sources(self) -> None:
        assert all(isinstance(s, SubstackSource) for s in SUBSTACK_INVESTING_SOURCES)

    def test_bases_are_unique(self) -> None:
        bases = [s.base for s in SUBSTACK_INVESTING_SOURCES]
        assert len(set(bases)) == len(bases), "duplicate base host in the curated list"

    def test_display_names_are_unique(self) -> None:
        names = [s.name for s in SUBSTACK_INVESTING_SOURCES]
        assert len(set(names)) == len(names), "duplicate display name in the curated list"

    def test_bases_are_plausible_hosts(self) -> None:
        for s in SUBSTACK_INVESTING_SOURCES:
            assert "://" not in s.base, f"{s.name}: base must be a bare host, not a URL"
            assert "/" not in s.base, f"{s.name}: base must not contain a path"
            assert _HOST_RE.match(s.base), f"{s.name}: implausible host {s.base!r}"
            assert "." in s.base, f"{s.name}: host must be dotted (custom or *.substack.com)"

    def test_fields_are_well_typed(self) -> None:
        for s in SUBSTACK_INVESTING_SOURCES:
            assert s.name and isinstance(s.name, str)
            assert s.sector and isinstance(s.sector, str)
            assert isinstance(s.paywall, bool)

    def test_url_property_is_https(self) -> None:
        for s in SUBSTACK_INVESTING_SOURCES:
            assert s.url == f"https://{s.base}"

    def test_broad_sector_coverage(self) -> None:
        sectors = {s.sector for s in SUBSTACK_INVESTING_SOURCES}
        # The user explicitly wants "all sectors" — require real breadth.
        assert len(sectors) >= 8, f"only {len(sectors)} sectors: {sorted(sectors)}"

    def test_covers_key_sectors(self) -> None:
        sectors = {s.sector for s in SUBSTACK_INVESTING_SOURCES}
        for required in ("Semiconductors", "Biotech & Healthcare", "Energy & Industrials"):
            assert required in sectors, f"missing required sector {required!r}"

    def test_no_sector_dominates(self) -> None:
        from collections import Counter

        counts = Counter(s.sector for s in SUBSTACK_INVESTING_SOURCES)
        # No single sector should swamp the list (keeps it diversified).
        assert max(counts.values()) <= 14, f"a sector dominates: {counts.most_common(3)}"

    def test_semianalysis_is_present(self) -> None:
        # The reference publication the user named explicitly.
        assert any("semianalysis" in s.base for s in SUBSTACK_INVESTING_SOURCES)


class TestSelection:
    def test_sectors_are_distinct_and_ordered(self) -> None:
        secs = sectors()
        assert len(secs) == len(set(secs)), "sectors() must not repeat a label"
        # First label is the first entry's sector (curated order preserved).
        assert secs[0] == SUBSTACK_INVESTING_SOURCES[0].sector

    def test_by_sector_filters_exactly(self) -> None:
        for sec in sectors():
            picked = by_sector(sec)
            assert picked, f"{sec!r} should have at least one publication"
            assert all(s.sector == sec for s in picked)

    def test_by_sector_partitions_the_list(self) -> None:
        total = sum(len(by_sector(sec)) for sec in sectors())
        assert total == len(SUBSTACK_INVESTING_SOURCES)

    def test_by_sector_unknown_is_empty(self) -> None:
        assert by_sector("Nonexistent Sector") == ()

    def test_select_sources_defaults_to_all(self) -> None:
        assert select_sources() == SUBSTACK_INVESTING_SOURCES

    def test_select_sources_limit_caps(self) -> None:
        picked = select_sources(limit=5)
        assert len(picked) == 5
        assert picked == SUBSTACK_INVESTING_SOURCES[:5]

    def test_select_sources_sector_and_limit(self) -> None:
        sec = SUBSTACK_INVESTING_SOURCES[0].sector
        picked = select_sources(sector=sec, limit=2)
        assert len(picked) <= 2
        assert all(s.sector == sec for s in picked)

    def test_select_sources_unknown_sector_is_empty(self) -> None:
        assert select_sources(sector="Nope") == ()


class TestCli:
    """The ``--list`` path is network-free, so it is safe to exercise in unit tests."""

    def test_list_all_publications(self) -> None:
        from typer.testing import CliRunner

        from scrapeforge.cli import app

        result = CliRunner().invoke(app, ["community", "scrape-substacks", "--list"])
        assert result.exit_code == 0
        assert "50 publication(s) selected" in result.stdout

    def test_list_filtered_by_sector(self) -> None:
        from typer.testing import CliRunner

        from scrapeforge.cli import app

        sec = "Semiconductors"
        n = len(by_sector(sec))
        result = CliRunner().invoke(
            app, ["community", "scrape-substacks", "--sector", sec, "--list"]
        )
        assert result.exit_code == 0
        assert f"{n} publication(s) selected" in result.stdout

    def test_unknown_sector_exits_nonzero(self) -> None:
        from typer.testing import CliRunner

        from scrapeforge.cli import app

        result = CliRunner().invoke(
            app, ["community", "scrape-substacks", "--sector", "Nope", "--list"]
        )
        assert result.exit_code == 1


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

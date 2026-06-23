"""Curated investing-Substack source list — deep dives into companies & sectors.

A hand-picked, **live-verified** catalogue of 50 Substack publications that do what
SemiAnalysis does for chips — rigorous, fundamentals-driven deep dives — but spread
across every sector an investor cares about (software, value/special-sits, financials
& fintech, energy & industrials, biotech & healthcare, and single-sector specialists
in gaming, defense, crypto, cleantech/EVs and CPG, plus China/global and big-tech context).

Why this lives here (and not in ``engine.py`` or a central registry)
-------------------------------------------------------------------
Adding a curated source list is **extension by addition** (CLAUDE.md §2, Invariant #16):
one new file in the community package.  Nothing here edits a shared seam file.  The
:class:`~scrapeforge.scrapers.community.substack.SubstackScraper` already self-registers
for ``*.substack.com`` + custom domains; this module just names the publications to point
it at and provides selection helpers the community CLI uses.

How it is consumed
------------------
These are *publication* hosts, not single-post URLs.  Two paths consume them:

1. **On-demand CLI** — ``community scrape-substacks`` drives
   ``SubstackScraper.scrape_publication`` (offset-paginated archive discovery + per-post
   fetch) over the selected publications and writes results to a JSONL sink.

2. **Scheduled ingestion** — ``seed_sources`` (bottom of this module) upserts the
   curated list into the ``sources`` table as community ``Source`` rows with
   ``params["platform"] = "substack"``.  The scheduler detects that field and routes
   each source to the ``INGEST`` queue instead of the single-URL ``JOB`` queue.  The
   ``community_ingest_worker`` then calls ``scrape_publication`` on each source, archives
   raw payloads (claim-check), and persists parsed articles via ``PostgresSink``.

Verification
------------
Every ``base`` host below was confirmed live (2026-06-22) against the public Substack
archive endpoint ``GET https://<base>/api/v1/archive?sort=new&limit=N`` returning HTTP
200 with a non-empty JSON array AND a recent post (the same contract :mod:`substack`
scrapes).  Custom
domains and ``*.substack.com`` subdomains that 301-redirect were followed to their
canonical host, which is what is stored here.  See ``docs/research/substack-investing-sources.md``.

The ``paywall`` flag is an *informational hint* (the newest post's ``audience`` was a
paid tier at verification time) — it is **not** a gate.  The scraper still filters
public vs. paid per-post at fetch time (Invariant #15); a paid-leaning publication
still yields its free posts.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SubstackSource:
    """One curated Substack publication.

    Attributes:
        name:    Human-readable publication name (unique within the list).
        base:    Canonical host with no scheme/path (e.g. ``newsletter.semianalysis.com``
                 or ``thechipletter.substack.com``).  This is what gets handed to
                 ``SubstackScraper.scrape_publication``.
        sector:  Coarse sector bucket (used by the CLI ``--sector`` filter & list balance).
        paywall: Informational hint — ``True`` if the publication leans paid.  Not a gate.
    """

    name: str
    base: str
    sector: str
    paywall: bool = False

    @property
    def url(self) -> str:
        """Full ``https://`` URL for the publication host."""
        return f"https://{self.base}"


# ---------------------------------------------------------------------------
# The curated 50 — grouped by sector, all live-verified 2026-06-22.
# ---------------------------------------------------------------------------
SUBSTACK_INVESTING_SOURCES: tuple[SubstackSource, ...] = (
    # --- Semiconductors & hardware (the SemiAnalysis lane) ------------------
    SubstackSource("SemiAnalysis", "newsletter.semianalysis.com", "Semiconductors", True),
    SubstackSource("Fabricated Knowledge", "www.fabricatedknowledge.com", "Semiconductors", True),
    SubstackSource("Chipstrat", "www.chipstrat.com", "Semiconductors"),
    SubstackSource("The Chip Letter", "thechipletter.substack.com", "Semiconductors"),
    SubstackSource("Asianometry", "www.asianometry.com", "Semiconductors"),
    # --- Software, internet & tech equities --------------------------------
    SubstackSource("App Economy Insights", "www.appeconomyinsights.com", "Software & Internet"),
    SubstackSource("Clouded Judgement", "cloudedjudgement.substack.com", "Software & Internet"),
    SubstackSource(
        "The Wolf of Harcourt Street", "www.thewolfofharcourtstreet.com", "Software & Internet"
    ),
    SubstackSource(
        "Rijnberk InvestInsights", "rijnberkinvestinsights.substack.com", "Software & Internet"
    ),
    SubstackSource("TechFund", "www.techinvestments.io", "Software & Internet", True),
    SubstackSource("Not Boring", "www.notboring.co", "Software & Internet", True),
    # --- Fundamental / value equity research -------------------------------
    SubstackSource("MBI Deep Dives", "mbideepdives.substack.com", "Equity Research", True),
    SubstackSource("StockOpine", "www.stockopine.com", "Equity Research"),
    SubstackSource("TSOH Investment Research", "thescienceofhitting.com", "Equity Research", True),
    SubstackSource("Best Anchor Stocks", "www.bestanchorstocks.com", "Equity Research", True),
    SubstackSource("Yet Another Value Blog", "www.yetanothervalueblog.com", "Equity Research"),
    SubstackSource("Kingswell", "www.kingswell.io", "Equity Research"),
    SubstackSource(
        "The Intrinsic Investor", "theintrinsicinvestor.substack.com", "Equity Research"
    ),
    SubstackSource("Invariant", "invariant.substack.com", "Equity Research"),
    SubstackSource("Clark Square Capital", "www.clarksquarecapital.com", "Equity Research", True),
    SubstackSource("Eagle Point Capital", "eaglepointcapital.substack.com", "Equity Research"),
    SubstackSource(
        "Special Situation Investing", "specialsituationinvesting.substack.com", "Equity Research"
    ),
    SubstackSource("Investment Talk", "www.investmenttalk.co", "Equity Research"),
    SubstackSource("The Finance Corner", "thefinancecorner.substack.com", "Equity Research"),
    # --- Growth & thematic --------------------------------------------------
    SubstackSource("Citrini Research", "www.citriniresearch.com", "Growth & Thematic", True),
    SubstackSource(
        "Growth Stock Deep Dives", "growthstockdeepdives.substack.com", "Growth & Thematic"
    ),
    SubstackSource("The Generalist", "www.generalist.com", "Growth & Thematic", True),
    # --- Forensic, short & governance --------------------------------------
    SubstackSource("The Bear Cave", "thebearcave.substack.com", "Forensic & Short"),
    SubstackSource("NonGAAP Investing", "www.nongaap.com", "Forensic & Short", True),
    SubstackSource("Security Analysis", "www.securityanalysis.org", "Forensic & Short", True),
    # --- Financials & fintech ----------------------------------------------
    SubstackSource("Net Interest", "www.netinterest.co", "Financials & Fintech", True),
    SubstackSource(
        "Fintech Business Weekly",
        "fintechbusinessweekly.substack.com",
        "Financials & Fintech",
        True,
    ),
    SubstackSource(
        "The Fintech Blueprint", "thefintechblueprint.substack.com", "Financials & Fintech"
    ),
    # --- Energy, commodities & industrials ---------------------------------
    SubstackSource("Doomberg", "newsletter.doomberg.com", "Energy & Industrials", True),
    SubstackSource("HFI Research", "www.hfir.com", "Energy & Industrials", True),
    SubstackSource("Open Insights", "www.openinsightscap.com", "Energy & Industrials"),
    SubstackSource("Super-Spiked (Arjun Murti)", "arjunmurti.substack.com", "Energy & Industrials"),
    SubstackSource(
        "Construction Physics", "www.construction-physics.com", "Energy & Industrials", True
    ),
    # --- Biotech & healthcare ----------------------------------------------
    SubstackSource("BowTiedBiotech", "bowtiedbiotech.substack.com", "Biotech & Healthcare", True),
    SubstackSource(
        "Matt Gamber's Biotech", "mattbiotech.substack.com", "Biotech & Healthcare", True
    ),
    SubstackSource("Biotech Blueprint", "www.biotechblueprint.com", "Biotech & Healthcare"),
    SubstackSource("Biotech Analysis: 0 to 1", "adus.substack.com", "Biotech & Healthcare"),
    SubstackSource("Hartaj Singh (pharma)", "hartajsingh1.substack.com", "Biotech & Healthcare"),
    # --- Single-sector specialists (gaming, defense, crypto, cleantech, CPG) -
    SubstackSource("Naavik (games industry)", "naavik.substack.com", "Sector Specialists"),
    SubstackSource("The Merge (defense)", "themerge.substack.com", "Sector Specialists"),
    SubstackSource("DeFi Education", "defieducation.substack.com", "Sector Specialists", True),
    SubstackSource("CleanTechnica", "cleantechnica.substack.com", "Sector Specialists", True),
    SubstackSource("Snaxshot (CPG)", "www.snaxshot.com", "Sector Specialists"),
    # --- China & big-tech context ------------------------------------------
    SubstackSource("Baiguan (China business)", "www.baiguan.news", "Global & Big Tech", True),
    SubstackSource("Big Technology", "www.bigtechnology.com", "Global & Big Tech"),
)


# ---------------------------------------------------------------------------
# Selection helpers — the community CLI uses these to choose what to scrape.
# ---------------------------------------------------------------------------


def sectors() -> tuple[str, ...]:
    """Return the distinct sector labels in first-seen (list) order."""
    seen: dict[str, None] = {}
    for s in SUBSTACK_INVESTING_SOURCES:
        seen.setdefault(s.sector, None)
    return tuple(seen)


def by_sector(sector: str) -> tuple[SubstackSource, ...]:
    """Return the curated publications whose ``sector`` matches *sector* exactly."""
    return tuple(s for s in SUBSTACK_INVESTING_SOURCES if s.sector == sector)


def select_sources(
    *,
    sector: str | None = None,
    limit: int | None = None,
) -> tuple[SubstackSource, ...]:
    """Pick curated publications to scrape.

    Args:
        sector: If given, keep only publications in this sector (exact match).
        limit:  If given, cap the result to the first *limit* publications.

    Returns:
        The selected publications, in curated (sector-grouped) order.
    """
    items = SUBSTACK_INVESTING_SOURCES if sector is None else by_sector(sector)
    return items if limit is None else tuple(items[:limit])


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

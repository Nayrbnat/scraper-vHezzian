"""Bundled sample articles so the digest prototype runs fully standalone.

These stand in for the scraped corpus (``Article``) until a real run has populated the store.
Swap ``sample_articles()`` for the real provider (JSONL/Postgres) via the CLI ``--source`` flag.
"""

from __future__ import annotations

from datetime import UTC, datetime

from scrapeforge.core.models import Article


def _article(url: str, title: str, content: str, *, days_ago: int, source_domain: str) -> Article:
    return Article(
        url=url,
        title=title,
        content=content,
        publish_date=datetime(2026, 6, 22, tzinfo=UTC).replace(day=22 - days_ago),
        metadata={"source_domain": source_domain, "bucket": "public"},
    )


def sample_articles() -> list[Article]:
    """A small spread covering companies, themes, and topics (for the seeded Dee profile)."""
    return [
        _article(
            "https://www.reuters.com/technology/anthropic-funding-2026",
            "Anthropic raises new round at a higher valuation",
            "Anthropic has closed a new funding round, the company said, extending its cash "
            "runway as competition in AI infrastructure intensifies. The raise underscores "
            "continued investor appetite for foundation-model labs despite a broader pullback "
            "in late-stage venture funding. Proceeds will go toward compute and safety research, "
            "according to people familiar with the matter, as the company scales its enterprise "
            "offerings and expands internationally across several new markets this year.",
            days_ago=0,
            source_domain="reuters.com",
        ),
        _article(
            "https://www.bloomberg.com/news/stripe-fintech-expansion",
            "Stripe expands into new fintech lending products",
            "Stripe is moving deeper into financial services with a set of lending products "
            "aimed at small businesses, a push that puts it in closer competition with banks. "
            "The fintech giant framed the move as a natural extension of its payments network. "
            "Analysts said the expansion could meaningfully grow take-rate over time, though it "
            "also raises the company's exposure to credit risk and evolving regulation in the "
            "consumer-finance space across multiple jurisdictions worldwide.",
            days_ago=1,
            source_domain="bloomberg.com",
        ),
        _article(
            "https://techcrunch.com/ai-infrastructure-datacenter-buildout",
            "AI infrastructure spending drives a record data-center buildout",
            "Hyperscalers and startups alike are pouring capital into AI infrastructure, with "
            "data-center construction reaching record levels this year. The theme has become one "
            "of the defining investment narratives of the cycle, spanning power, networking, and "
            "custom silicon. Investors are increasingly looking past the model layer to the picks "
            "and shovels — energy, cooling, and chips — that underpin the entire build-out and "
            "could prove more durable than any single application.",
            days_ago=2,
            source_domain="techcrunch.com",
        ),
        _article(
            "https://www.ft.com/content/defense-tech-funding-rounds",
            "Defense tech sees a wave of new funding rounds",
            "Venture funding into defense technology accelerated this quarter, with several "
            "large rounds closing as governments increase procurement budgets. The defense tech "
            "theme has drawn investors seeking exposure to autonomy, sensing, and dual-use AI. "
            "Founders said demand signals from allied governments were the strongest they had "
            "seen, even as questions remain about long sales cycles and the political durability "
            "of elevated spending over the coming decade.",
            days_ago=3,
            source_domain="ft.com",
        ),
        _article(
            "https://www.wsj.com/articles/quarterly-ma-activity-regulation",
            "M&A activity rebounds as regulators signal a softer stance",
            "Mergers and acquisitions picked up sharply as dealmakers grew more confident that "
            "regulators would take a softer line on consolidation. The shift in regulation has "
            "unlocked transactions that had been on hold, particularly in technology and media. "
            "Bankers cautioned that the rebound remains uneven and concentrated in a handful of "
            "sectors, but said the pipeline of announced deals was the deepest in two years and "
            "pointed to a busier second half.",
            days_ago=4,
            source_domain="wsj.com",
        ),
        _article(
            "https://www.cnbc.com/markets/consumer-staples-earnings",
            "Consumer staples post steady earnings amid soft demand",
            "A range of consumer-staples companies reported steady but unspectacular earnings, "
            "with management teams pointing to soft volumes offset by pricing. The results offered "
            "little for growth investors but reassured those seeking defensive exposure. Few "
            "companies changed full-year guidance, and commentary focused on input costs and "
            "promotional intensity heading into the back half of the year across most categories.",
            days_ago=2,
            source_domain="cnbc.com",
        ),
    ]

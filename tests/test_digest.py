"""Tests for the Hezzian digest prototype — schemas, matching, render, preview send, CLI."""

from __future__ import annotations

from datetime import UTC, datetime

from typer.testing import CliRunner

from scrapeforge.core.models import Article
from scrapeforge.digest.cli import digest_app
from scrapeforge.digest.matcher import _matches, build_digest, summarize
from scrapeforge.digest.models import Digest, DigestPreferences, Subscriber
from scrapeforge.digest.render import render_email
from scrapeforge.digest.samples import sample_articles
from scrapeforge.digest.sender import PreviewEmailSender
from scrapeforge.digest.service import load_subscriber, make_digest


def _sub(**prefs) -> Subscriber:
    return Subscriber(
        id="dee",
        name="Dee",
        email="nayrbnat@gmail.com",
        preferences=DigestPreferences(**prefs),
    )


def _art(title: str, content: str, *, days_ago: int = 0, url: str = "https://x.com/a") -> Article:
    return Article(
        url=url,
        title=title,
        content=content,
        publish_date=datetime(2026, 6, 20, tzinfo=UTC).replace(day=20 - days_ago),
        metadata={"bucket": "public"},
    )


# --------------------------------------------------------------------------- models / JSON


class TestModels:
    def test_subscriber_round_trips_json(self) -> None:
        sub = _sub(portfolio_companies=["Stripe"], investment_themes=["fintech"])
        again = Subscriber.model_validate_json(sub.model_dump_json())
        assert again == sub
        assert again.email == "nayrbnat@gmail.com"

    def test_seed_file_loads(self) -> None:
        sub = load_subscriber("data/subscribers/dee.json")
        assert sub.id == "dee" and sub.name == "Dee"
        assert "Anthropic" in sub.preferences.portfolio_companies

    def test_invalid_email_rejected(self) -> None:
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Subscriber(id="x", name="X", email="not-an-email")

    def test_preferences_defaults(self) -> None:
        p = DigestPreferences()
        assert p.cadence == "daily"
        assert p.data_types == ["news"]
        assert p.max_items_per_section == 5

    def test_extra_field_forbidden(self) -> None:
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DigestPreferences(unexpected="nope")


# --------------------------------------------------------------------------- matcher


class TestMatcher:
    def test_whole_word_matching(self) -> None:
        assert _matches("the ai boom", "AI")
        assert not _matches("a mountain trail", "AI")  # substring must not match

    def test_summary_truncation(self) -> None:
        short = "Brief."
        assert summarize(short) == short
        long = "First sentence here. " + "word " * 200
        out = summarize(long, max_chars=100)
        assert len(out) <= 160 and out.startswith("First sentence here.")

    def test_sections_and_priority(self) -> None:
        sub = _sub(portfolio_companies=["Stripe"], investment_themes=["fintech"])
        # mentions BOTH a company and a theme -> goes to the higher-priority portfolio section
        arts = [_art("Stripe news", "Stripe does fintech things " * 30)]
        digest = build_digest(sub, arts)
        assert [s.key for s in digest.sections] == ["portfolio"]
        assert digest.sections[0].items[0].matched_on == ["Stripe"]

    def test_non_matching_dropped(self) -> None:
        sub = _sub(portfolio_companies=["Stripe"])
        digest = build_digest(sub, [_art("Weather", "It rained a lot today " * 30)])
        assert digest.is_empty

    def test_cap_and_recency_order(self) -> None:
        sub = _sub(investment_themes=["fintech"], max_items_per_section=2)
        arts = [
            _art(f"fintech {i}", "fintech " * 30, days_ago=i, url=f"https://x.com/{i}")
            for i in range(5)
        ]
        digest = build_digest(sub, arts)
        items = digest.sections[0].items
        assert len(items) == 2  # capped
        assert items[0].title == "fintech 0"  # most recent first (days_ago=0)

    def test_built_from_samples(self) -> None:
        sub = load_subscriber("data/subscribers/dee.json")
        digest = build_digest(sub, sample_articles())
        keys = {s.key for s in digest.sections}
        assert {"portfolio", "themes"} <= keys
        assert digest.total_items >= 4


# --------------------------------------------------------------------------- render


class TestRender:
    def test_render_contains_content(self) -> None:
        sub = load_subscriber("data/subscribers/dee.json")
        digest = build_digest(sub, sample_articles())
        email = render_email(digest)
        assert "Hi Dee" in email.html and "Hi Dee" in email.text
        assert "Hezzian" in email.subject and digest.period in email.subject
        assert "Anthropic" in email.html

    def test_empty_digest_renders_gracefully(self) -> None:
        digest = build_digest(_sub(), [])  # no prefs, no articles
        email = render_email(digest)
        assert "No new updates" in email.text
        assert "(0 updates)" in email.subject

    def test_html_escapes_article_content(self) -> None:
        sub = _sub(portfolio_companies=["Stripe"])
        art = _art("<script>alert(1)</script> Stripe", "Stripe & friends " * 40)
        email = render_email(build_digest(sub, [art]))
        assert "<script>alert(1)</script>" not in email.html  # raw tag must not survive
        assert "&lt;script&gt;" in email.html  # escaped form present


# --------------------------------------------------------------------------- sender / service


class TestPreviewSender:
    def test_writes_html_file(self, tmp_path) -> None:
        sub = load_subscriber("data/subscribers/dee.json")
        _digest, email = make_digest(sub, "sample")
        PreviewEmailSender(tmp_path).send(sub.email, email)
        out = tmp_path / "nayrbnat_at_gmail_com.html"
        assert out.exists()
        assert "Hezzian" in out.read_text(encoding="utf-8")


class TestCli:
    def test_preview_command(self, tmp_path) -> None:
        result = CliRunner().invoke(
            digest_app,
            ["preview", "--subscriber", "data/subscribers/dee.json", "--out-dir", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert "Built digest for Dee" in result.output
        assert (tmp_path / "nayrbnat_at_gmail_com.html").exists()

    def test_send_without_creds_fails_cleanly(self, monkeypatch) -> None:
        # Hermetic: disable the CLI's .env auto-load so a developer's real local .env can't
        # repopulate the creds (which would defeat this test AND actually send an email).
        monkeypatch.setattr("scrapeforge.digest.cli._load_dotenv", lambda: None)
        for var in ("DIGEST_SMTP_USER", "DIGEST_SMTP_PASSWORD", "DIGEST_FROM"):
            monkeypatch.delenv(var, raising=False)
        result = CliRunner().invoke(digest_app, ["send", "--yes"])
        assert result.exit_code == 1
        assert "Cannot send" in result.output


def test_digest_is_pydantic_serializable() -> None:
    sub = load_subscriber("data/subscribers/dee.json")
    digest = build_digest(sub, sample_articles())
    # the whole Digest round-trips as JSON (the forward-compatible wire format)
    again = Digest.model_validate_json(digest.model_dump_json())
    assert again.total_items == digest.total_items

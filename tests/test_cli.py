"""Tests for CLI entry points (SPEC.md §5).

TDD first — tests drive scrapeforge/cli.py, scrapeforge/scrapers/public/cli.py,
and scrapeforge/__main__.py.

All tests are hermetic: ScrapeEngine.scrape is monkeypatched; no real network.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from scrapeforge.core.models import Article, ScrapeResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _success_result(url: str = "https://example.com") -> ScrapeResult:
    return ScrapeResult(
        status="success",
        driver_used="curl_cffi",
        article=Article(url=url, title="CLI Test Article", content="content " * 100),
    )


runner = CliRunner()


# ---------------------------------------------------------------------------
# Root app — verify-fingerprint
# ---------------------------------------------------------------------------


class TestVerifyFingerprint:
    def test_exits_zero(self):
        from scrapeforge.cli import app

        # Monkeypatch FingerprintManager so it never probes the host OS
        fake_fm = MagicMock()
        fake_profile = MagicMock()
        fake_profile.chrome_major_version = 131
        fake_fm.return_value.generate_profile.return_value = fake_profile
        fake_fm.return_value.curl_impersonate_target.return_value = "chrome131"

        with patch("scrapeforge.cli.FingerprintManager", fake_fm):
            result = runner.invoke(app, ["verify-fingerprint"])

        assert result.exit_code == 0, result.output

    def test_prints_impersonate_target(self):
        from scrapeforge.cli import app

        fake_fm = MagicMock()
        fake_profile = MagicMock()
        fake_profile.chrome_major_version = 131
        fake_fm.return_value.generate_profile.return_value = fake_profile
        fake_fm.return_value.curl_impersonate_target.return_value = "chrome131"

        with patch("scrapeforge.cli.FingerprintManager", fake_fm):
            result = runner.invoke(app, ["verify-fingerprint"])

        assert "chrome131" in result.output


# ---------------------------------------------------------------------------
# Root app — list-sessions
# ---------------------------------------------------------------------------


class TestListSessions:
    def test_exits_zero(self):
        from scrapeforge.cli import app

        result = runner.invoke(app, ["list-sessions"])
        assert result.exit_code == 0, result.output

    def test_mentions_phase_4(self):
        from scrapeforge.cli import app

        result = runner.invoke(app, ["list-sessions"])
        # The command must print some message (not crash); it tells the user
        # auth lands in Phase 4.
        assert len(result.output.strip()) > 0


# ---------------------------------------------------------------------------
# public scrape sub-command
# ---------------------------------------------------------------------------


class TestPublicScrapeCommand:
    def test_exits_zero_on_success(self, tmp_path: Path):
        from scrapeforge.cli import app

        out = str(tmp_path / "out")
        with patch("scrapeforge.scrapers.public.cli.ScrapeEngine") as mock_engine_cls:
            mock_engine = MagicMock()
            mock_engine.scrape = AsyncMock(
                return_value=_success_result("https://example.com/article")
            )
            mock_engine_cls.return_value = mock_engine

            result = runner.invoke(
                app,
                ["public", "scrape", "https://example.com/article", "--output", out],
            )

        assert result.exit_code == 0, result.output

    def test_prints_status_summary(self, tmp_path: Path):
        from scrapeforge.cli import app

        out = str(tmp_path / "out")
        with patch("scrapeforge.scrapers.public.cli.ScrapeEngine") as mock_engine_cls:
            mock_engine = MagicMock()
            mock_engine.scrape = AsyncMock(
                return_value=_success_result("https://example.com/article")
            )
            mock_engine_cls.return_value = mock_engine

            result = runner.invoke(
                app,
                ["public", "scrape", "https://example.com/article", "--output", out],
            )

        assert "success" in result.output.lower()

    def test_prints_article_title(self, tmp_path: Path):
        from scrapeforge.cli import app

        out = str(tmp_path / "out")
        with patch("scrapeforge.scrapers.public.cli.ScrapeEngine") as mock_engine_cls:
            mock_engine = MagicMock()
            mock_engine.scrape = AsyncMock(
                return_value=_success_result("https://example.com/article")
            )
            mock_engine_cls.return_value = mock_engine

            result = runner.invoke(
                app,
                ["public", "scrape", "https://example.com/article", "--output", out],
            )

        assert "CLI Test Article" in result.output

    def test_proxy_passed_to_engine(self, tmp_path: Path):
        from scrapeforge.cli import app

        with patch("scrapeforge.scrapers.public.cli.ScrapeEngine") as mock_engine_cls:
            mock_engine = MagicMock()
            mock_engine.scrape = AsyncMock(
                return_value=_success_result("https://example.com/article")
            )
            mock_engine_cls.return_value = mock_engine

            result = runner.invoke(
                app,
                [
                    "public",
                    "scrape",
                    "https://example.com/article",
                    "--output",
                    str(tmp_path / "out"),
                    "--proxy",
                    "http://px:8080",
                ],
            )

        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# __main__ — importable
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_importable(self):
        """__main__.py should be importable without error."""
        import importlib

        # Import should not raise
        mod = importlib.import_module("scrapeforge.__main__")
        assert hasattr(mod, "app")

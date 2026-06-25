"""map_to_profile: project a hezzian row into scraper_news.user_profiles fields (pure)."""

from __future__ import annotations

from scrapeforge.pipeline.user_sync import HezzianUserRow, map_to_profile


def _row(interests, **kw):
    base = {
        "clerk_user_id": "user_abc",
        "email": "a@e.com",
        "interests": interests,
        "investor_type": "student",
        "experience_level": "1-3y",
        "risk_tolerance": "low",
        "primary_objective": "growth",
        "time_horizon": "1-3y",
    }
    base.update(kw)
    return HezzianUserRow(**base)


def test_maps_dict_interests() -> None:
    row = _row(
        {
            "regions": ["US"],
            "sectors": ["Tech"],
            "asset_classes": ["Healthcare"],
            "watch_tickers": ["NVDA"],
        }
    )
    out = map_to_profile(row)
    assert out["user_id"] == "user_abc"
    assert out["email"] == "a@e.com"
    assert out["portfolio"] == ["NVDA"]
    assert out["sectors"] == ["Tech", "Healthcare"]
    assert out["focus"] == "student; low risk; growth; 1-3y; US"


def test_maps_json_string_interests() -> None:
    row = _row('{"sectors": ["AI"], "watch_tickers": ["MSFT"]}')
    out = map_to_profile(row)
    assert out["portfolio"] == ["MSFT"]
    assert out["sectors"] == ["AI"]


def test_missing_or_bad_interests_yields_empty_lists() -> None:
    assert map_to_profile(_row(None))["portfolio"] == []
    assert map_to_profile(_row("not json"))["sectors"] == []
    assert map_to_profile(_row({}))["portfolio"] == []


def test_focus_none_when_all_optionals_missing() -> None:
    row = _row(
        {}, investor_type=None, risk_tolerance=None, primary_objective=None, time_horizon=None
    )
    assert map_to_profile(row)["focus"] is None


def test_asyncpg_url_transform() -> None:
    from scrapeforge.pipeline.user_sync import _asyncpg

    # Neon URL with libpq-only query params -> asyncpg dialect, params stripped.
    assert (
        _asyncpg("postgresql://u:p@h/db?sslmode=require&channel_binding=require")
        == "postgresql+asyncpg://u:p@h/db"
    )
    # Already-asyncpg URL is left intact (minus any query string).
    assert _asyncpg("postgresql+asyncpg://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"

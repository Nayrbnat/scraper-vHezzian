"""Tests for scrapeforge.utils.humanize — pure math humanization utilities.

TDD: written before implementation.  All functions are pure (no I/O, no sleep).

Coverage targets:
- MousePathGenerator: endpoints exact, path non-linear, point count sane.
- ScrollSimulator: covers full page height, tuples are (int, int).
- DelayEngine: return values within documented ranges.
- No time.sleep calls — these return delays, never apply them.
"""

from __future__ import annotations

import random
import statistics
import time

import pytest

from scrapeforge.utils.humanize import DelayEngine, MousePathGenerator, ScrollSimulator

# ---------------------------------------------------------------------------
# MousePathGenerator
# ---------------------------------------------------------------------------


class TestMousePathGenerator:
    def setup_method(self) -> None:
        random.seed(42)
        self.gen = MousePathGenerator()

    def test_first_point_is_start(self) -> None:
        path = self.gen.generate(start=(0, 0), end=(100, 200), duration_ms=500)
        assert path[0] == (0, 0)

    def test_last_point_is_end(self) -> None:
        path = self.gen.generate(start=(0, 0), end=(100, 200), duration_ms=500)
        assert path[-1] == (100, 200)

    def test_minimum_two_points(self) -> None:
        """Even a very short duration produces at least 2 points."""
        path = self.gen.generate(start=(10, 10), end=(20, 20), duration_ms=10)
        assert len(path) >= 2

    def test_point_count_proportional_to_duration(self) -> None:
        """Longer duration -> more points (~duration_ms // 16)."""
        short_path = self.gen.generate(start=(0, 0), end=(500, 500), duration_ms=160)
        long_path = self.gen.generate(start=(0, 0), end=(500, 500), duration_ms=800)
        assert len(long_path) > len(short_path)

    def test_path_is_non_linear(self) -> None:
        """The intermediate points must NOT all lie on the straight line start->end.

        A collinear check: for start=(0,0), end=(100,100), all purely linear
        intermediate points would satisfy x == y.  With bezier + jitter this
        should not hold for ALL intermediate points.
        """
        random.seed(7)
        path = self.gen.generate(start=(0, 0), end=(100, 100), duration_ms=500)
        intermediates = path[1:-1]
        all_collinear = all(abs(x - y) < 2 for x, y in intermediates)
        assert not all_collinear, "Path is perfectly linear — bezier/jitter is not applied"

    def test_points_are_integer_tuples(self) -> None:
        path = self.gen.generate(start=(5, 10), end=(200, 300), duration_ms=300)
        for pt in path:
            assert isinstance(pt, tuple), f"Expected tuple, got {type(pt)}"
            assert len(pt) == 2
            assert isinstance(pt[0], int) and isinstance(pt[1], int), (
                f"Expected (int, int), got {pt}"
            )

    def test_different_start_end(self) -> None:
        """A variety of start/end coordinates produce valid paths."""
        pairs = [
            ((0, 0), (1920, 1080)),
            ((100, 200), (50, 50)),
            ((500, 500), (500, 500)),  # same point edge case
        ]
        for start, end in pairs:
            random.seed(0)
            path = self.gen.generate(start=start, end=end, duration_ms=200)
            assert path[0] == start
            assert path[-1] == end


# ---------------------------------------------------------------------------
# ScrollSimulator
# ---------------------------------------------------------------------------


class TestScrollSimulator:
    def setup_method(self) -> None:
        random.seed(42)
        self.sim = ScrollSimulator()

    def test_returns_list_of_tuples(self) -> None:
        scrolls = self.sim.generate_scrolls(page_height=3000, viewport_height=800)
        assert isinstance(scrolls, list)
        assert len(scrolls) > 0
        for item in scrolls:
            assert isinstance(item, tuple) and len(item) == 2

    def test_covers_full_page(self) -> None:
        """The cumulative scroll_y increments must cover the scrollable area."""
        page_h, viewport_h = 3000, 800
        scrolls = self.sim.generate_scrolls(page_height=page_h, viewport_height=viewport_h)
        total = sum(dy for dy, _ in scrolls)
        # Should cover at least the scrollable distance (page_h - viewport_h)
        scrollable = page_h - viewport_h
        assert total >= scrollable, (
            f"Total scroll {total} does not cover scrollable area {scrollable}"
        )

    def test_delay_ms_is_positive(self) -> None:
        scrolls = self.sim.generate_scrolls(page_height=2000, viewport_height=600)
        for _, delay in scrolls:
            assert delay > 0, "Delay must be positive"

    def test_scroll_increments_are_positive(self) -> None:
        scrolls = self.sim.generate_scrolls(page_height=2000, viewport_height=600)
        for dy, _ in scrolls:
            assert dy > 0, "Each scroll increment must be positive"

    def test_variable_increments(self) -> None:
        """Scroll steps should vary — not all identical (human-like behaviour)."""
        random.seed(99)
        scrolls = self.sim.generate_scrolls(page_height=5000, viewport_height=800)
        steps = [dy for dy, _ in scrolls]
        # There should be at least some variation
        assert len(set(steps)) > 1, "All scroll steps are identical — no jitter applied"

    def test_page_equals_viewport_produces_minimal_scrolls(self) -> None:
        """If page == viewport, there is nothing to scroll — result should be empty or minimal."""
        scrolls = self.sim.generate_scrolls(page_height=800, viewport_height=800)
        # Either no scrolls, or a single 0/negligible scroll
        total = sum(dy for dy, _ in scrolls)
        assert total == 0 or len(scrolls) <= 1


# ---------------------------------------------------------------------------
# DelayEngine
# ---------------------------------------------------------------------------


class TestDelayEngine:
    """Tests for staticmethod delay generators — all return floats in seconds."""

    def test_reading_pause_range(self) -> None:
        """reading_pause() is always in [2.0, 6.0]."""
        random.seed(0)
        for _ in range(200):
            d = DelayEngine.reading_pause()
            assert 2.0 <= d <= 6.0, f"reading_pause out of range: {d}"

    def test_reading_pause_returns_float(self) -> None:
        d = DelayEngine.reading_pause()
        assert isinstance(d, float)

    def test_action_delay_default_range(self) -> None:
        """action_delay() with defaults is in [0.5, 2.0] seconds."""
        random.seed(1)
        for _ in range(200):
            d = DelayEngine.action_delay()
            assert 0.5 <= d <= 2.0, f"action_delay out of range: {d}"

    def test_action_delay_custom_range(self) -> None:
        """action_delay accepts min_ms / max_ms overrides."""
        random.seed(2)
        for _ in range(100):
            d = DelayEngine.action_delay(min_ms=100, max_ms=300)
            assert 0.1 <= d <= 0.3, f"action_delay (custom) out of range: {d}"

    def test_typing_interval_non_negative(self) -> None:
        """typing_interval is always >= 0 (clamped Gaussian)."""
        random.seed(3)
        for _ in range(500):
            d = DelayEngine.typing_interval()
            assert d >= 0.0, f"typing_interval is negative: {d}"

    def test_typing_interval_mean_approx(self) -> None:
        """typing_interval mean over many samples ≈ 80ms (0.08 s) ± tolerance."""
        random.seed(42)
        samples = [DelayEngine.typing_interval(mean_ms=80.0, std_ms=5.0) for _ in range(2000)]
        mean_s = statistics.mean(samples)
        # With std=5ms the clamping effect is minimal; allow ±10ms tolerance
        assert abs(mean_s - 0.08) < 0.01, (
            f"typing_interval mean {mean_s:.4f}s deviates more than 10ms from 0.08s"
        )

    def test_typing_interval_custom_params(self) -> None:
        """typing_interval respects custom mean_ms / std_ms."""
        random.seed(7)
        samples = [DelayEngine.typing_interval(mean_ms=150.0, std_ms=10.0) for _ in range(500)]
        mean_s = statistics.mean(samples)
        assert abs(mean_s - 0.15) < 0.02

    def test_no_sleep_called(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Invariant #4: humanize utilities RETURN delays; they never call time.sleep."""
        calls: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: calls.append(s))

        DelayEngine.reading_pause()
        DelayEngine.action_delay()
        DelayEngine.typing_interval()

        assert calls == [], f"time.sleep was called with: {calls}"

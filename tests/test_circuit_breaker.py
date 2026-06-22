"""Tests for scrapeforge.core.circuit_breaker (SPEC.md §3.19a).

TDD: tests are written before the implementation.

All tests use an injected fake clock so time is fully deterministic.
"""

from __future__ import annotations

from scrapeforge.core.circuit_breaker import CircuitBreaker

# ---------------------------------------------------------------------------
# Fake clock
# ---------------------------------------------------------------------------


class FakeClock:
    """Mutable monotonic clock for deterministic testing."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_allow_returns_true_for_unknown_domain():
    """A domain with no recorded failures is always allowed."""
    cb = CircuitBreaker(threshold=5, pause_minutes=30, window_minutes=10, clock=FakeClock())
    assert cb.allow("new.com") is True


def test_under_threshold_failures_do_not_trip():
    """Fewer than threshold failures within the window do NOT pause the domain."""
    clock = FakeClock(start=0.0)
    cb = CircuitBreaker(threshold=5, pause_minutes=30, window_minutes=10, clock=clock)

    for _ in range(4):
        cb.record("site.com", success=False)

    assert cb.allow("site.com") is True


def test_exactly_threshold_failures_trips_breaker():
    """Exactly threshold failures within the window trips the breaker."""
    clock = FakeClock(start=0.0)
    cb = CircuitBreaker(threshold=5, pause_minutes=30, window_minutes=10, clock=clock)

    for _ in range(5):
        cb.record("site.com", success=False)

    assert cb.allow("site.com") is False


def test_tripped_domain_stays_paused_until_pause_elapses():
    """allow() returns False while the pause window has not yet elapsed."""
    clock = FakeClock(start=0.0)
    pause_minutes = 30
    cb = CircuitBreaker(threshold=5, pause_minutes=pause_minutes, window_minutes=10, clock=clock)

    for _ in range(5):
        cb.record("site.com", success=False)

    # Just before the pause expires:
    clock.advance(pause_minutes * 60 - 1)
    assert cb.allow("site.com") is False


def test_paused_domain_recovers_after_pause_elapses():
    """allow() returns True once the pause window has elapsed."""
    clock = FakeClock(start=0.0)
    pause_minutes = 30
    cb = CircuitBreaker(threshold=5, pause_minutes=pause_minutes, window_minutes=10, clock=clock)

    for _ in range(5):
        cb.record("site.com", success=False)

    clock.advance(pause_minutes * 60 + 1)
    assert cb.allow("site.com") is True


def test_success_resets_failure_count():
    """A success clears accumulated failures so further fails don't immediately trip."""
    clock = FakeClock(start=0.0)
    cb = CircuitBreaker(threshold=5, pause_minutes=30, window_minutes=10, clock=clock)

    for _ in range(4):
        cb.record("site.com", success=False)

    cb.record("site.com", success=True)

    # Need threshold failures again to trip:
    for _ in range(4):
        cb.record("site.com", success=False)

    assert cb.allow("site.com") is True


def test_success_clears_active_pause():
    """A success after a trip clears the pause so allow() returns True immediately."""
    clock = FakeClock(start=0.0)
    cb = CircuitBreaker(threshold=5, pause_minutes=30, window_minutes=10, clock=clock)

    for _ in range(5):
        cb.record("site.com", success=False)

    assert cb.allow("site.com") is False

    cb.record("site.com", success=True)

    assert cb.allow("site.com") is True


def test_failures_outside_window_are_dropped():
    """Failures older than window_minutes do not accumulate toward the threshold."""
    clock = FakeClock(start=0.0)
    window_minutes = 10
    cb = CircuitBreaker(threshold=5, pause_minutes=30, window_minutes=window_minutes, clock=clock)

    # Record 4 failures, then advance past the window.
    for _ in range(4):
        cb.record("site.com", success=False)

    clock.advance(window_minutes * 60 + 1)

    # One more failure after the window — old ones are now stale.
    cb.record("site.com", success=False)

    # Should NOT have tripped (only 1 in-window failure):
    assert cb.allow("site.com") is True


def test_failures_spanning_window_boundary_dont_trip():
    """Failures that straddle the window boundary: only in-window ones count."""
    clock = FakeClock(start=0.0)
    window_minutes = 10
    threshold = 5
    cb = CircuitBreaker(
        threshold=threshold, pause_minutes=30, window_minutes=window_minutes, clock=clock
    )

    # 3 failures at t=0
    for _ in range(3):
        cb.record("site.com", success=False)

    # Advance past the window
    clock.advance(window_minutes * 60 + 1)

    # 2 new failures at t = window+1 (only these are in-window)
    for _ in range(2):
        cb.record("site.com", success=False)

    # Total in-window = 2, threshold = 5 → not tripped
    assert cb.allow("site.com") is True


def test_multiple_domains_are_independent():
    """Tripping one domain does not affect a different domain's state."""
    clock = FakeClock(start=0.0)
    cb = CircuitBreaker(threshold=5, pause_minutes=30, window_minutes=10, clock=clock)

    for _ in range(5):
        cb.record("bad.com", success=False)

    assert cb.allow("bad.com") is False
    assert cb.allow("good.com") is True

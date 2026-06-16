"""Reactive per-domain circuit-breaker policy (SPEC.md §3.19a).

``CircuitBreaker`` is the *reactive* resilience gate — it pauses a domain
after repeated failures so the engine stops hammering a target that is blocking
us.  The *proactive* politeness gate (minimum interval between requests) lives
in ``RateLimiter`` (§3.19).

This module is *pure synchronous* — no I/O.  The engine calls:

- ``allow(domain)`` before dispatching a scrape request.
- ``record(domain, success)`` after each attempt.

Clock is injected for deterministic testing::

    from scrapeforge.core.circuit_breaker import CircuitBreaker

    cb = CircuitBreaker(threshold=5, pause_minutes=30, window_minutes=10)
    if not cb.allow('site.com'):
        # domain is paused — skip
        ...
    cb.record('site.com', success=False)
"""

from __future__ import annotations

import time
from collections.abc import Callable


class CircuitBreaker:
    """Per-domain trip policy.

    Invariants (SPEC.md §3.19a):
    - ``allow(domain)`` returns ``False`` while a domain is paused; ``True``
      otherwise.  An expired pause is lazily cleared on the next ``allow()``
      call.
    - Trip rule: ``>= threshold`` failures within *window_minutes* pause the
      domain for *pause_minutes*.
    - ``record(domain, success=True)`` resets all failure state (and clears any
      active pause) for that domain.
    - State is per-domain; one domain's failures never affect another.

    Args:
        threshold: Number of failures within the window that trips the breaker.
        pause_minutes: How long to pause the domain after tripping.
        window_minutes: Rolling window in which failures are counted.
        clock: Zero-argument callable returning a monotonic float timestamp.
            Defaults to ``time.monotonic``; inject a fake for tests.
    """

    def __init__(
        self,
        threshold: int = 5,
        pause_minutes: int = 30,
        window_minutes: int = 10,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._pause_s = pause_minutes * 60.0
        self._window_s = window_minutes * 60.0
        self._clock = clock
        # Per-domain state dict:
        #   {
        #     'failures': list[float],   # monotonic timestamps of recent failures
        #     'paused_until': float,     # 0.0 means not paused
        #   }
        self._state: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def allow(self, domain: str) -> bool:
        """Return ``True`` unless *domain* is currently paused.

        An expired pause is cleared lazily so ``allow()`` stays a pure read
        from the caller's perspective (no side-effects that matter).
        """
        state = self._state.get(domain)
        if state is None:
            return True

        paused_until = state.get("paused_until", 0.0)
        if paused_until == 0.0:
            return True

        now = self._clock()
        if now >= paused_until:
            # Pause has elapsed; clear it lazily.
            state["paused_until"] = 0.0
            return True

        return False

    def record(self, domain: str, success: bool) -> None:
        """Record the outcome of a scrape attempt for *domain*.

        On *success*: reset all failure state (failures list + any active pause).
        On *failure*: append a timestamp, drop stale timestamps outside the
        rolling window, and trip the breaker if the in-window count meets the
        threshold.
        """
        if domain not in self._state:
            self._state[domain] = {"failures": [], "paused_until": 0.0}

        state = self._state[domain]

        if success:
            state["failures"] = []
            state["paused_until"] = 0.0
            return

        now = self._clock()
        state["failures"].append(now)

        # Drop failures outside the rolling window.
        cutoff = now - self._window_s
        state["failures"] = [t for t in state["failures"] if t > cutoff]

        if len(state["failures"]) >= self._threshold:
            state["paused_until"] = now + self._pause_s

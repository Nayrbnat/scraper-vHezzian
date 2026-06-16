"""Proactive per-domain rate limiter (SPEC.md §3.19).

``RateLimiter`` is the *proactive* politeness gate — it ensures each domain is
not hit more frequently than its configured interval.  The *reactive* policy
(pausing after failures) lives in ``CircuitBreaker`` (§3.19a).

Clock and sleep are injected for deterministic testing::

    from scrapeforge.core.rate_limiter import RateLimiter

    rl = RateLimiter(default_interval_s=1.0)
    await rl.acquire('example.com')  # blocks until the slot is free
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class RateLimiter:
    """Per-domain minimum-interval gate.

    Invariants (SPEC.md §3.19):
    - ``acquire(domain)`` is FIFO per domain (each domain has its own
      ``asyncio.Lock``).
    - Premium domains enforce a hard floor via ``overrides``.
    - Defaults come from constructor args (engine reads them from ``Settings``).

    Args:
        default_interval_s: Minimum seconds between requests for any domain that
            does not appear in *overrides*.
        overrides: Per-domain interval overrides.  Keys are domain strings;
            values are interval seconds.  Useful for premium floors (e.g.
            ``{'ft.com': 60.0}``).
        clock: Zero-argument callable returning a monotonic float timestamp.
            Defaults to ``time.monotonic``; inject a fake for tests.
        sleep: Async callable ``(seconds: float) -> Awaitable``.  Defaults to
            ``asyncio.sleep``; inject a fake for tests.
    """

    def __init__(
        self,
        default_interval_s: float = 1.0,
        overrides: dict[str, float] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._default = default_interval_s
        self._overrides: dict[str, float] = dict(overrides) if overrides else {}
        self._clock = clock
        self._sleep = sleep
        # next_allowed[domain] = monotonic timestamp at which the next request may start.
        self._next_allowed: dict[str, float] = {}
        # One lock per domain to serialise concurrent acquires for the same domain (FIFO).
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def acquire(self, domain: str) -> None:
        """Block until *domain*'s rate-limit slot is free, then reserve the next slot.

        Per-domain ``asyncio.Lock`` ensures FIFO ordering when multiple
        coroutines wait for the same domain.
        """
        lock = self._get_lock(domain)
        async with lock:
            now = self._clock()
            wait = self._next_allowed.get(domain, 0.0) - now
            if wait > 0:
                await self._sleep(wait)
            # Update _next_allowed AFTER the optional sleep, using the
            # post-sleep clock reading so the next caller's wait is exact.
            self._next_allowed[domain] = self._clock() + self._interval(domain)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _interval(self, domain: str) -> float:
        """Return the configured interval for *domain*."""
        return self._overrides.get(domain, self._default)

    def _get_lock(self, domain: str) -> asyncio.Lock:
        """Return (creating if necessary) the per-domain lock."""
        if domain not in self._locks:
            self._locks[domain] = asyncio.Lock()
        return self._locks[domain]

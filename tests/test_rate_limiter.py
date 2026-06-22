"""Tests for scrapeforge.core.rate_limiter (SPEC.md §3.19).

TDD: tests are written before the implementation.

All tests use injected fake clock + fake async sleep so there is no real delay.
"""

from __future__ import annotations

import pytest

from scrapeforge.core.rate_limiter import RateLimiter

# ---------------------------------------------------------------------------
# Fake time primitives
# ---------------------------------------------------------------------------


class FakeClock:
    """Mutable monotonic clock for deterministic testing.

    Call ``advance(n)`` to move time forward by ``n`` seconds.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class FakeSleep:
    """Coroutine-returning callable that records requested waits.

    Each ``await fake_sleep(n)`` appends ``n`` to ``self.calls`` and advances
    the paired ``FakeClock`` so subsequent ``clock()`` calls see time passing.
    """

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self._clock.advance(seconds)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_acquire_returns_immediately():
    """The first acquire on a domain should not sleep at all."""
    clock = FakeClock(start=100.0)
    fake_sleep = FakeSleep(clock)

    rl = RateLimiter(default_interval_s=1.0, clock=clock, sleep=fake_sleep)
    await rl.acquire("example.com")

    assert fake_sleep.calls == [], "First acquire must not sleep"


@pytest.mark.asyncio
async def test_second_acquire_waits_interval():
    """Second acquire on the same domain sleeps for the default interval."""
    clock = FakeClock(start=0.0)
    fake_sleep = FakeSleep(clock)

    interval = 2.0
    rl = RateLimiter(default_interval_s=interval, clock=clock, sleep=fake_sleep)

    await rl.acquire("site.com")
    await rl.acquire("site.com")

    # Exactly one sleep call, approximately equal to the interval.
    assert len(fake_sleep.calls) == 1
    assert abs(fake_sleep.calls[0] - interval) < 0.01


@pytest.mark.asyncio
async def test_different_domains_are_independent():
    """Acquiring different domains does not cross-pollinate their timing state."""
    clock = FakeClock(start=0.0)
    fake_sleep = FakeSleep(clock)

    rl = RateLimiter(default_interval_s=5.0, clock=clock, sleep=fake_sleep)

    await rl.acquire("alpha.com")
    # A fresh domain should not be delayed by alpha.com's slot:
    await rl.acquire("beta.com")

    assert fake_sleep.calls == [], "Second domain's first acquire must not sleep"


@pytest.mark.asyncio
async def test_per_domain_override_enforced():
    """Domain-specific override takes precedence over default_interval_s."""
    clock = FakeClock(start=0.0)
    fake_sleep = FakeSleep(clock)

    premium_interval = 60.0
    rl = RateLimiter(
        default_interval_s=1.0,
        overrides={"ft.com": premium_interval},
        clock=clock,
        sleep=fake_sleep,
    )

    await rl.acquire("ft.com")
    await rl.acquire("ft.com")

    assert len(fake_sleep.calls) == 1
    assert abs(fake_sleep.calls[0] - premium_interval) < 0.01


@pytest.mark.asyncio
async def test_non_overridden_domain_uses_default():
    """A domain not in overrides uses the default interval."""
    clock = FakeClock(start=0.0)
    fake_sleep = FakeSleep(clock)

    rl = RateLimiter(
        default_interval_s=3.0,
        overrides={"special.com": 99.0},
        clock=clock,
        sleep=fake_sleep,
    )

    await rl.acquire("normal.com")
    await rl.acquire("normal.com")

    assert len(fake_sleep.calls) == 1
    assert abs(fake_sleep.calls[0] - 3.0) < 0.01


@pytest.mark.asyncio
async def test_no_sleep_when_enough_time_has_passed():
    """If the clock has advanced past the next-allowed time, no sleep is needed."""
    clock = FakeClock(start=0.0)
    fake_sleep = FakeSleep(clock)

    interval = 2.0
    rl = RateLimiter(default_interval_s=interval, clock=clock, sleep=fake_sleep)

    await rl.acquire("fast.com")
    # Manually advance clock well beyond the interval before second acquire:
    clock.advance(interval + 10.0)
    await rl.acquire("fast.com")

    assert fake_sleep.calls == [], "Should not sleep when cooldown has already elapsed"


@pytest.mark.asyncio
async def test_multiple_sequential_acquires_accumulate_correctly():
    """Three consecutive acquires sleep twice, each for the interval."""
    clock = FakeClock(start=0.0)
    fake_sleep = FakeSleep(clock)

    interval = 1.0
    rl = RateLimiter(default_interval_s=interval, clock=clock, sleep=fake_sleep)

    for _ in range(3):
        await rl.acquire("seq.com")

    assert len(fake_sleep.calls) == 2
    for call in fake_sleep.calls:
        assert abs(call - interval) < 0.01


@pytest.mark.asyncio
async def test_concurrent_acquires_same_domain_are_serialized():
    """The per-domain lock serializes concurrent coroutines: N tasks → N-1 sleeps.

    This proves FIFO/serialization by observable behavior: gather() launches all
    coroutines concurrently, but the lock forces them to queue up, so every task
    after the first must sleep for exactly one interval.  No real delay occurs
    because FakeSleep advances the FakeClock instead of calling asyncio.sleep.
    """
    import asyncio

    n = 5
    interval = 2.0
    clock = FakeClock(start=0.0)
    fake_sleep = FakeSleep(clock)

    rl = RateLimiter(default_interval_s=interval, clock=clock, sleep=fake_sleep)

    # Launch n concurrent acquires on the same domain.
    await asyncio.gather(*[rl.acquire("concurrent.com") for _ in range(n)])

    # The first task through the lock sleeps 0 times; each subsequent one sleeps once.
    assert len(fake_sleep.calls) == n - 1, (
        f"Expected {n - 1} sleeps for {n} concurrent acquires, got {len(fake_sleep.calls)}"
    )
    for recorded_wait in fake_sleep.calls:
        assert abs(recorded_wait - interval) < 0.01, (
            f"Each sleep should be ~{interval}s, got {recorded_wait}"
        )

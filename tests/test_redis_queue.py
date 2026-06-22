"""Tests for core/queue/redis_queue.py — RedisQueue implementation.

TDD: written before the implementation exists (RED phase).
Uses fakeredis.aioredis.FakeRedis as the injected async Redis client.
asyncio_mode=auto (configured in pyproject.toml), so no explicit @pytest.mark.asyncio.

Key behaviours under test:
- publish → reserve FIFO
- reserve increments attempts (in-memory only; processing list retains original bytes)
- reserve on empty → None
- ack removes from processing list (reliable-queue pattern, LREM on exact original bytes)
- requeue re-delivers with current attempts count stored
- dead_letter lands in <queue>:dlq list
- consume_once retry → DLQ over max_retries ∈ {0, 1, 2}
- size reflects the main list only
- crash-recovery shape: after reserve (no ack), ORIGINAL bytes sit in <queue>:processing
- payload round-trip stability: nested unordered payload survives reserve→requeue→reserve→ack
- QueueError wraps raw RedisError (typed exceptions)
- @integration: full cycle against real redis at redis://localhost:6399/0

Move command used: LMOVE (fakeredis supports it; verified experimentally).
"""

from __future__ import annotations

import json

import fakeredis.aioredis
import pytest

from scrapeforge.core.queue.base import QueueError
from scrapeforge.core.queue.redis_queue import RedisQueue

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def redis_client() -> fakeredis.aioredis.FakeRedis:
    """Fresh async FakeRedis client, auto-closed after each test."""
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


@pytest.fixture
async def queue(redis_client: fakeredis.aioredis.FakeRedis) -> RedisQueue:
    """RedisQueue backed by a fresh FakeRedis client."""
    return RedisQueue(redis_client, dlq_suffix=":dlq")


# ---------------------------------------------------------------------------
# publish → reserve FIFO
# ---------------------------------------------------------------------------


async def test_publish_reserve_fifo(queue: RedisQueue) -> None:
    """Messages are delivered in FIFO order (first published, first reserved)."""
    id_a = await queue.publish("jobs", {"seq": 1})
    id_b = await queue.publish("jobs", {"seq": 2})
    id_c = await queue.publish("jobs", {"seq": 3})

    msg_a = await queue.reserve("jobs")
    msg_b = await queue.reserve("jobs")
    msg_c = await queue.reserve("jobs")

    assert msg_a is not None
    assert msg_b is not None
    assert msg_c is not None
    assert msg_a.id == id_a
    assert msg_b.id == id_b
    assert msg_c.id == id_c
    assert msg_a.payload == {"seq": 1}
    assert msg_b.payload == {"seq": 2}
    assert msg_c.payload == {"seq": 3}


# ---------------------------------------------------------------------------
# publish returns a non-empty unique string id
# ---------------------------------------------------------------------------


async def test_publish_returns_str_id(queue: RedisQueue) -> None:
    """publish() returns a non-empty string id."""
    msg_id = await queue.publish("q", {"x": 1})
    assert isinstance(msg_id, str)
    assert msg_id


async def test_publish_returns_unique_ids(queue: RedisQueue) -> None:
    """Each publish returns a unique id."""
    ids = [await queue.publish("q", {"n": i}) for i in range(5)]
    assert len(set(ids)) == 5


# ---------------------------------------------------------------------------
# reserve increments attempts (in-memory; processing bytes stay original)
# ---------------------------------------------------------------------------


async def test_reserve_increments_attempts(queue: RedisQueue) -> None:
    """reserve() increments message.attempts to 1 on first call."""
    await queue.publish("q", {"x": 1})
    msg = await queue.reserve("q")
    assert msg is not None
    assert msg.attempts == 1


async def test_reserve_increments_attempts_on_requeue(queue: RedisQueue) -> None:
    """After requeue, reserve increments attempts again (1 -> 2)."""
    await queue.publish("q", {"x": 1})
    msg = await queue.reserve("q")
    assert msg is not None
    assert msg.attempts == 1
    await queue.requeue(msg)
    msg2 = await queue.reserve("q")
    assert msg2 is not None
    assert msg2.attempts == 2


# ---------------------------------------------------------------------------
# reserve on empty queue -> None
# ---------------------------------------------------------------------------


async def test_reserve_empty_returns_none(queue: RedisQueue) -> None:
    """reserve() returns None when the queue has no waiting messages."""
    result = await queue.reserve("empty_queue")
    assert result is None


async def test_reserve_empty_after_ack(queue: RedisQueue) -> None:
    """After acking the only message the queue is empty."""
    await queue.publish("q", {})
    msg = await queue.reserve("q")
    assert msg is not None
    await queue.ack(msg)
    assert await queue.reserve("q") is None


# ---------------------------------------------------------------------------
# ack removes from processing list (LREM on exact original bytes)
# ---------------------------------------------------------------------------


async def test_ack_clears_processing_list(
    queue: RedisQueue, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """After ack, the processing list for the queue is empty."""
    await queue.publish("q", {"v": 42})
    msg = await queue.reserve("q")
    assert msg is not None

    # Before ack: item must be in the processing list
    proc_len_before = await redis_client.llen("q:processing")
    assert proc_len_before == 1

    await queue.ack(msg)

    # After ack: processing list is empty
    proc_len_after = await redis_client.llen("q:processing")
    assert proc_len_after == 0


async def test_ack_prevents_redelivery(queue: RedisQueue) -> None:
    """After ack, the message cannot be reserved again."""
    await queue.publish("q", {"v": 42})
    msg = await queue.reserve("q")
    assert msg is not None
    await queue.ack(msg)
    assert await queue.size("q") == 0
    assert await queue.reserve("q") is None


# ---------------------------------------------------------------------------
# requeue re-delivers with attempts preserved in storage
# ---------------------------------------------------------------------------


async def test_requeue_redelivers(queue: RedisQueue) -> None:
    """requeue() puts the message back; reserve() increments attempts again."""
    await queue.publish("q", {"v": 1})
    msg = await queue.reserve("q")
    assert msg is not None
    assert msg.attempts == 1
    await queue.requeue(msg)

    msg2 = await queue.reserve("q")
    assert msg2 is not None
    assert msg2.id == msg.id
    assert msg2.attempts == 2


async def test_requeue_removes_from_processing(
    queue: RedisQueue, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """After requeue, the item is in the main queue, not the processing list."""
    await queue.publish("q", {"v": 1})
    msg = await queue.reserve("q")
    assert msg is not None
    await queue.requeue(msg)

    # Processing list must be empty after requeue
    assert await redis_client.llen("q:processing") == 0
    # Main queue has the item back
    assert await queue.size("q") == 1


# ---------------------------------------------------------------------------
# dead_letter lands in <queue>:dlq
# ---------------------------------------------------------------------------


async def test_dead_letter_lands_in_dlq(
    queue: RedisQueue, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """dead_letter() pushes the message into '<queue>:dlq'."""
    await queue.publish("jobs", {"url": "https://x.com"})
    msg = await queue.reserve("jobs")
    assert msg is not None
    await queue.dead_letter(msg, "too many retries")

    dlq_len = await redis_client.llen("jobs:dlq")
    assert dlq_len == 1

    raw = await redis_client.lrange("jobs:dlq", 0, -1)
    entry = json.loads(raw[0])
    assert entry["payload"] == {"url": "https://x.com"}
    assert entry["error"] == "too many retries"


async def test_dead_letter_clears_processing(
    queue: RedisQueue, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """After dead_letter, the processing list is empty."""
    await queue.publish("jobs", {})
    msg = await queue.reserve("jobs")
    assert msg is not None
    await queue.dead_letter(msg, "err")

    assert await redis_client.llen("jobs:processing") == 0
    assert await queue.size("jobs") == 0
    assert await queue.reserve("jobs") is None


# ---------------------------------------------------------------------------
# size reflects the main list only
# ---------------------------------------------------------------------------


async def test_size_empty(queue: RedisQueue) -> None:
    """size() is 0 for a queue that has never received a message."""
    assert await queue.size("nonexistent") == 0


async def test_size_after_publish(queue: RedisQueue) -> None:
    """size() reflects waiting (not reserved) messages."""
    await queue.publish("q", {})
    await queue.publish("q", {})
    assert await queue.size("q") == 2


async def test_size_decreases_after_reserve(queue: RedisQueue) -> None:
    """Reserved messages are NOT counted in size()."""
    await queue.publish("q", {})
    await queue.publish("q", {})
    await queue.reserve("q")
    assert await queue.size("q") == 1


async def test_size_back_after_requeue(queue: RedisQueue) -> None:
    """Requeued messages are counted again in size()."""
    await queue.publish("q", {})
    msg = await queue.reserve("q")
    assert msg is not None
    assert await queue.size("q") == 0
    await queue.requeue(msg)
    assert await queue.size("q") == 1


# ---------------------------------------------------------------------------
# consume_once — concrete helper from base (inherited, not overridden)
# ---------------------------------------------------------------------------


async def test_consume_once_success_acks_and_returns_true(queue: RedisQueue) -> None:
    """consume_once() calls handler, acks the message, and returns True on success."""
    received: list[dict] = []

    async def handler(payload: dict) -> None:
        received.append(payload)

    await queue.publish("q", {"k": "v"})
    result = await queue.consume_once("q", handler, max_retries=3)

    assert result is True
    assert received == [{"k": "v"}]
    assert await queue.size("q") == 0
    assert await queue.reserve("q") is None


async def test_consume_once_empty_returns_false(queue: RedisQueue) -> None:
    """consume_once() returns False when the queue is empty."""
    called = False

    async def handler(payload: dict) -> None:
        nonlocal called
        called = True

    result = await queue.consume_once("q", handler, max_retries=3)
    assert result is False
    assert not called


@pytest.mark.parametrize("max_retries", [0, 1, 2])
async def test_consume_once_dead_letters_after_max_retries_plus_one_calls(
    redis_client: fakeredis.aioredis.FakeRedis,
    max_retries: int,
) -> None:
    """Handler runs exactly max_retries+1 times; then the message is dead-lettered.

    Handler call count == max_retries + 1; final placement: DLQ (main queue empty).
    """
    q = RedisQueue(redis_client, dlq_suffix=":dlq")
    call_count = 0

    async def bad_handler(payload: dict) -> None:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        raise RuntimeError("permanent failure")

    await q.publish("q", {"x": 1})

    for _ in range(max_retries + 1):
        result = await q.consume_once("q", bad_handler, max_retries=max_retries)
        assert result is True

    assert call_count == max_retries + 1

    # Message ended up in the DLQ exactly once
    dlq_len = await redis_client.llen("q:dlq")
    assert dlq_len == 1
    # Main queue is empty
    assert await q.size("q") == 0


# ---------------------------------------------------------------------------
# Crash-recovery shape — ORIGINAL bytes (attempts=0) sit in processing after reserve
# ---------------------------------------------------------------------------


async def test_crash_recovery_shape_item_in_processing_after_reserve(
    queue: RedisQueue, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """After reserve() (no ack/requeue/dead_letter), the ORIGINAL bytes remain in
    <queue>:processing — immutable, so a reaper can recover without data loss.

    Key property of the redesigned reserve():
    - The processing list entry has attempts=0 (original, as published).
    - The returned Message has attempts=1 (in-memory increment only).
    - LREM in ack/requeue/dead_letter uses the stored original bytes, guaranteeing
      an exact byte match with what LMOVE placed there.
    """
    await queue.publish("q", {"work": True})
    msg = await queue.reserve("q")
    assert msg is not None
    assert msg.attempts == 1  # in-memory value is incremented

    # Simulate a worker crash: do NOT call ack/requeue/dead_letter.
    # The item must still be in the processing list.
    processing_items = await redis_client.lrange("q:processing", 0, -1)
    assert len(processing_items) == 1

    # The main queue is now empty (item was moved to processing)
    assert await queue.size("q") == 0

    # CRITICAL: the processing list entry retains the ORIGINAL bytes (attempts=0)
    # This guarantees ack/requeue/dead_letter can LREM the exact same bytes.
    entry = json.loads(processing_items[0])
    assert entry["id"] == msg.id
    assert entry["attempts"] == 0  # original, NOT incremented


# ---------------------------------------------------------------------------
# Payload round-trip stability — nested unordered payload survives full cycle
# ---------------------------------------------------------------------------


async def test_payload_round_trip_stability(
    queue: RedisQueue, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    """A non-trivially-ordered nested payload survives reserve->requeue->reserve->ack
    with no orphan left in processing.

    Specifically verifies:
    - The returned payload is identical to the published payload.
    - LLEN(processing) == 0 after the full cycle (ack matched the exact bytes).
    - The queue drains cleanly (no ghost entries).
    """
    payload = {"b": 1, "a": 2, "nested": {"z": 1, "y": 2}}
    await queue.publish("q", payload)

    # First delivery
    msg1 = await queue.reserve("q")
    assert msg1 is not None
    assert msg1.payload == payload
    assert msg1.attempts == 1

    # Requeue (simulates transient failure)
    await queue.requeue(msg1)
    assert await redis_client.llen("q:processing") == 0

    # Second delivery
    msg2 = await queue.reserve("q")
    assert msg2 is not None
    assert msg2.id == msg1.id
    assert msg2.payload == payload
    assert msg2.attempts == 2

    # Ack — must match EXACT bytes in processing
    await queue.ack(msg2)

    # No orphans anywhere
    assert await redis_client.llen("q:processing") == 0
    assert await queue.size("q") == 0
    assert await queue.reserve("q") is None


# ---------------------------------------------------------------------------
# Typed exceptions — QueueError wraps RedisError
# ---------------------------------------------------------------------------


async def test_publish_raises_queue_error_on_redis_failure(
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    """publish() raises QueueError (not raw RedisError) when Redis fails."""
    from unittest.mock import AsyncMock, patch

    q = RedisQueue(redis_client)

    import redis.exceptions

    with (
        patch.object(
            redis_client, "rpush", new=AsyncMock(side_effect=redis.exceptions.RedisError("boom"))
        ),
        pytest.raises(QueueError),
    ):
        await q.publish("q", {"x": 1})


async def test_reserve_raises_queue_error_on_redis_failure(
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    """reserve() raises QueueError (not raw RedisError) when Redis fails."""
    from unittest.mock import AsyncMock, patch

    import redis.exceptions

    q = RedisQueue(redis_client)
    with (
        patch.object(
            redis_client, "lmove", new=AsyncMock(side_effect=redis.exceptions.RedisError("boom"))
        ),
        pytest.raises(QueueError),
    ):
        await q.reserve("q")


# ---------------------------------------------------------------------------
# Integration — real Redis at redis://localhost:6399/0
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_integration_full_cycle_real_redis() -> None:
    """Full publish->reserve->ack / requeue / dead_letter cycle on real Redis.

    Proves LMOVE/LREM byte semantics work on real redis-py (not just fakeredis).
    Skipped gracefully if Redis at localhost:6399 is unreachable.
    """
    import redis.asyncio
    import redis.exceptions

    client = redis.asyncio.from_url("redis://localhost:6399/0", decode_responses=False)
    try:
        await client.ping()
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError):
        await client.aclose()
        pytest.skip("Real Redis not reachable at redis://localhost:6399/0")

    # Use a unique prefix to avoid interference with other test runs
    import uuid

    prefix = f"inttest:{uuid.uuid4().hex[:8]}"
    q_name = f"{prefix}:jobs"

    q = RedisQueue(client)
    try:
        # --- ack path ---
        msg_id = await q.publish(q_name, {"url": "https://example.com"})
        msg = await q.reserve(q_name)
        assert msg is not None
        assert msg.id == msg_id
        assert msg.attempts == 1
        await q.ack(msg)
        assert await client.llen(f"{q_name}:processing") == 0
        assert await q.size(q_name) == 0

        # --- requeue path (attempts accumulate) ---
        await q.publish(q_name, {"retry": True})
        m1 = await q.reserve(q_name)
        assert m1 is not None and m1.attempts == 1
        await q.requeue(m1)
        assert await client.llen(f"{q_name}:processing") == 0

        m2 = await q.reserve(q_name)
        assert m2 is not None and m2.attempts == 2
        await q.ack(m2)
        assert await client.llen(f"{q_name}:processing") == 0

        # --- dead_letter path ---
        await q.publish(q_name, {"fatal": True})
        md = await q.reserve(q_name)
        assert md is not None
        await q.dead_letter(md, "unrecoverable")
        dlq_key = f"{q_name}:dlq"
        assert await client.llen(dlq_key) == 1
        raw_dlq = await client.lrange(dlq_key, 0, -1)
        entry = json.loads(raw_dlq[0])
        assert entry["error"] == "unrecoverable"
        assert entry["payload"] == {"fatal": True}
        assert await client.llen(f"{q_name}:processing") == 0

    finally:
        # Clean up all keys created by this test
        for suffix in ("", ":processing", ":dlq"):
            await client.delete(f"{q_name}{suffix}")
        await client.aclose()

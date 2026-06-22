"""Tests for core/queue — MessageQueue port and InMemoryMessageQueue implementation.

Written FIRST (TDD red phase). All tests must fail before implementation exists.
Uses asyncio_mode=auto (configured in pyproject.toml), so no explicit @pytest.mark.asyncio needed.
"""

from __future__ import annotations

import pytest

from scrapeforge.core.queue.base import Message, MessageQueue, QueueError
from scrapeforge.core.queue.memory import InMemoryMessageQueue

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def queue() -> InMemoryMessageQueue:
    """Fresh InMemoryMessageQueue with default DLQ suffix."""
    return InMemoryMessageQueue(dlq_suffix=":dlq")


# ---------------------------------------------------------------------------
# Message dataclass
# ---------------------------------------------------------------------------


def test_message_defaults() -> None:
    """Message.attempts defaults to 0."""
    msg = Message(id="abc", queue="jobs", payload={"url": "https://example.com"})
    assert msg.attempts == 0
    assert msg.id == "abc"
    assert msg.queue == "jobs"
    assert msg.payload == {"url": "https://example.com"}


# ---------------------------------------------------------------------------
# publish → reserve FIFO order
# ---------------------------------------------------------------------------


async def test_publish_reserve_fifo(queue: InMemoryMessageQueue) -> None:
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
# publish returns a message id (str)
# ---------------------------------------------------------------------------


async def test_publish_returns_id(queue: InMemoryMessageQueue) -> None:
    """publish() returns a non-empty string id."""
    msg_id = await queue.publish("q", {"x": 1})
    assert isinstance(msg_id, str)
    assert msg_id  # non-empty


async def test_publish_returns_unique_ids(queue: InMemoryMessageQueue) -> None:
    """Each publish call returns a unique id."""
    ids = [await queue.publish("q", {"n": i}) for i in range(5)]
    assert len(set(ids)) == 5


# ---------------------------------------------------------------------------
# reserve increments attempts
# ---------------------------------------------------------------------------


async def test_reserve_increments_attempts(queue: InMemoryMessageQueue) -> None:
    """reserve() increments message.attempts by 1 each time it is reserved."""
    await queue.publish("q", {"x": 1})
    msg = await queue.reserve("q")
    assert msg is not None
    assert msg.attempts == 1


# ---------------------------------------------------------------------------
# reserve on empty queue → None
# ---------------------------------------------------------------------------


async def test_reserve_empty_returns_none(queue: InMemoryMessageQueue) -> None:
    """reserve() returns None when the queue has no waiting messages."""
    result = await queue.reserve("empty_queue")
    assert result is None


async def test_reserve_empty_after_ack(queue: InMemoryMessageQueue) -> None:
    """After acking the only message the queue is empty."""
    await queue.publish("q", {})
    msg = await queue.reserve("q")
    assert msg is not None
    await queue.ack(msg)
    result = await queue.reserve("q")
    assert result is None


# ---------------------------------------------------------------------------
# ack removes in-flight
# ---------------------------------------------------------------------------


async def test_ack_removes_inflight(queue: InMemoryMessageQueue) -> None:
    """After ack, the message is not re-delivered."""
    await queue.publish("q", {"v": 42})
    msg = await queue.reserve("q")
    assert msg is not None
    await queue.ack(msg)
    # Queue is empty and in-flight is clear
    assert await queue.size("q") == 0
    assert await queue.reserve("q") is None


# ---------------------------------------------------------------------------
# requeue re-delivers with attempts preserved
# ---------------------------------------------------------------------------


async def test_requeue_redelivers(queue: InMemoryMessageQueue) -> None:
    """requeue() puts the message back so it can be reserved again.

    reserve() always increments attempts, so after requeue+reserve the count goes 1→2.
    The key property is that requeue itself does NOT increment; only reserve does.
    """
    await queue.publish("q", {"v": 1})
    msg = await queue.reserve("q")
    assert msg is not None
    assert msg.attempts == 1
    await queue.requeue(msg)

    msg2 = await queue.reserve("q")
    assert msg2 is not None
    assert msg2.id == msg.id
    # reserve() increments attempts each time: 1 → 2
    assert msg2.attempts == 2


async def test_requeue_then_reserve_increments_again(queue: InMemoryMessageQueue) -> None:
    """Each reserve() call increments attempts; requeue preserves the prior count."""
    await queue.publish("q", {})
    msg = await queue.reserve("q")  # attempts == 1
    assert msg is not None
    await queue.requeue(msg)
    msg2 = await queue.reserve("q")  # attempts == 2 (incremented by reserve)
    assert msg2 is not None
    assert msg2.attempts == 2


# ---------------------------------------------------------------------------
# dead_letter moves to DLQ
# ---------------------------------------------------------------------------


async def test_dead_letter_lands_in_dlq(queue: InMemoryMessageQueue) -> None:
    """dead_letter() moves the message into '<queue>:dlq'."""
    await queue.publish("jobs", {"url": "https://x.com"})
    msg = await queue.reserve("jobs")
    assert msg is not None
    await queue.dead_letter(msg, "too many retries")

    dead = queue.dead_letters("jobs")
    assert len(dead) == 1
    assert dead[0].id == msg.id
    assert dead[0].payload == {"url": "https://x.com"}


async def test_dead_letter_not_in_main_queue(queue: InMemoryMessageQueue) -> None:
    """After dead_letter, the message is no longer in the main queue or in-flight."""
    await queue.publish("jobs", {})
    msg = await queue.reserve("jobs")
    assert msg is not None
    await queue.dead_letter(msg, "error")
    assert await queue.size("jobs") == 0
    assert await queue.reserve("jobs") is None


async def test_dead_letters_inspectable(queue: InMemoryMessageQueue) -> None:
    """dead_letters(queue) returns the list of dead-lettered messages."""
    await queue.publish("q", {"n": 1})
    await queue.publish("q", {"n": 2})
    m1 = await queue.reserve("q")
    m2 = await queue.reserve("q")
    assert m1 is not None and m2 is not None
    await queue.dead_letter(m1, "err1")
    await queue.dead_letter(m2, "err2")

    dead = queue.dead_letters("q")
    assert len(dead) == 2
    payloads = {d.payload["n"] for d in dead}
    assert payloads == {1, 2}


# ---------------------------------------------------------------------------
# size reflects waiting count (not in-flight)
# ---------------------------------------------------------------------------


async def test_size_empty(queue: InMemoryMessageQueue) -> None:
    """size() is 0 for a queue that has never received a message."""
    assert await queue.size("nonexistent") == 0


async def test_size_after_publish(queue: InMemoryMessageQueue) -> None:
    """size() reflects the number of waiting (not reserved) messages."""
    await queue.publish("q", {})
    await queue.publish("q", {})
    assert await queue.size("q") == 2


async def test_size_decreases_after_reserve(queue: InMemoryMessageQueue) -> None:
    """Reserved messages are no longer counted in size()."""
    await queue.publish("q", {})
    await queue.publish("q", {})
    await queue.reserve("q")
    assert await queue.size("q") == 1


async def test_size_back_after_requeue(queue: InMemoryMessageQueue) -> None:
    """Requeued messages are counted again in size()."""
    await queue.publish("q", {})
    msg = await queue.reserve("q")
    assert msg is not None
    assert await queue.size("q") == 0
    await queue.requeue(msg)
    assert await queue.size("q") == 1


# ---------------------------------------------------------------------------
# consume_once — success path
# ---------------------------------------------------------------------------


async def test_consume_once_success_acks_and_returns_true(queue: InMemoryMessageQueue) -> None:
    """consume_once() calls handler, acks the message, and returns True on success."""
    received: list[dict] = []

    async def handler(payload: dict) -> None:
        received.append(payload)

    await queue.publish("q", {"k": "v"})
    result = await queue.consume_once("q", handler, max_retries=3)

    assert result is True
    assert received == [{"k": "v"}]
    # Message should be gone (acked)
    assert await queue.size("q") == 0
    assert await queue.reserve("q") is None


# ---------------------------------------------------------------------------
# consume_once — empty queue returns False
# ---------------------------------------------------------------------------


async def test_consume_once_empty_returns_false(queue: InMemoryMessageQueue) -> None:
    """consume_once() returns False when the queue is empty; handler is NOT called."""
    called = False

    async def handler(payload: dict) -> None:
        nonlocal called
        called = True

    result = await queue.consume_once("q", handler, max_retries=3)
    assert result is False
    assert not called


# ---------------------------------------------------------------------------
# consume_once — handler raises, attempts < max_retries → requeue
# ---------------------------------------------------------------------------


async def test_consume_once_requeues_on_error_below_max_retries(
    queue: InMemoryMessageQueue,
) -> None:
    """When handler raises and attempts < max_retries, message is requeued."""

    async def bad_handler(payload: dict) -> None:
        raise ValueError("transient error")

    await queue.publish("q", {"x": 1})
    result = await queue.consume_once("q", bad_handler, max_retries=3)

    assert result is True  # a message was processed (requeued is still processed)
    # message is back in the queue
    assert await queue.size("q") == 1
    msg = await queue.reserve("q")
    assert msg is not None
    assert msg.attempts == 2  # 1 from first reserve + 1 from second reserve


# ---------------------------------------------------------------------------
# consume_once — handler raises, attempts > max_retries → dead-letter
# (parametrized: handler runs exactly max_retries+1 times before DLQ)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("max_retries", [0, 1, 3])
async def test_consume_once_dead_letters_after_max_retries_plus_one_calls(
    max_retries: int,
) -> None:
    """Handler runs exactly max_retries+1 times total; on the next failure the message
    is dead-lettered (attempts > max_retries boundary).

    Boundary table:
      max_retries=0 → 1 total attempt  (0 retries, dead-letter after attempt 1)
      max_retries=1 → 2 total attempts (1 retry,   dead-letter after attempt 2)
      max_retries=3 → 4 total attempts (3 retries, dead-letter after attempt 4)
    """
    queue = InMemoryMessageQueue(dlq_suffix=":dlq")
    call_count = 0

    async def bad_handler(payload: dict) -> None:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        raise RuntimeError("permanent failure")

    await queue.publish("q", {"x": 1})

    # Drive consume_once until the message is dead-lettered.
    # It takes max_retries+1 handler invocations (each via one consume_once call).
    for _ in range(max_retries + 1):
        result = await queue.consume_once("q", bad_handler, max_retries=max_retries)
        assert result is True  # a message was processed each iteration

    # Handler was called exactly max_retries+1 times
    assert call_count == max_retries + 1
    # Message ended up in the DLQ exactly once
    dead = queue.dead_letters("q")
    assert len(dead) == 1
    assert dead[0].payload == {"x": 1}
    # Main queue is now empty
    assert await queue.size("q") == 0


# ---------------------------------------------------------------------------
# "lost message" — reserved but never ack/requeue/dead_letter
# ---------------------------------------------------------------------------


async def test_reserved_but_unacknowledged_message_is_not_redelivered(
    queue: InMemoryMessageQueue,
) -> None:
    """A message that is reserved but never ack'd, requeue'd, or dead-letter'd stays
    in-flight and is NOT redelivered by a subsequent reserve().

    This is intentional for the in-memory fake: it models a worker crash-before-ack.
    Durable backends (Redis adapter, W2) will need a visibility-timeout / recovery
    mechanism (e.g. BRPOPLPUSH + a reaper job) that this fake intentionally omits.
    """
    await queue.publish("q", {"work": True})
    reserved = await queue.reserve("q")
    assert reserved is not None

    # Simulate crash: never call ack / requeue / dead_letter.
    # A subsequent reserve must return None — the message is in-flight, not re-queued.
    second = await queue.reserve("q")
    assert second is None
    # size() counts only waiting messages; the in-flight one does not count.
    assert await queue.size("q") == 0


# ---------------------------------------------------------------------------
# QueueError is subclass of ScrapeForgeError
# ---------------------------------------------------------------------------


def test_queue_error_is_scrapeforge_error() -> None:
    """QueueError inherits from ScrapeForgeError."""
    from scrapeforge.exceptions import ScrapeForgeError

    err = QueueError("something went wrong")
    assert isinstance(err, ScrapeForgeError)


# ---------------------------------------------------------------------------
# MessageQueue is abstract
# ---------------------------------------------------------------------------


def test_message_queue_is_abstract() -> None:
    """MessageQueue cannot be instantiated directly (ABC)."""
    with pytest.raises(TypeError):
        MessageQueue()  # type: ignore[abstract]

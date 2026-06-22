"""RedisQueue — durable MessageQueue implementation using Redis reliable-queue pattern.

Each logical queue ``<name>`` uses two Redis lists:
- ``<name>``            — the main waiting list (FIFO via RPUSH / LMOVE LEFT)
- ``<name>:processing`` — the in-flight list (atomically moved here by ``reserve``)

Reliable-queue guarantee
------------------------
``reserve()`` is a SINGLE atomic ``LMOVE <queue> <queue>:processing LEFT RIGHT``.
The bytes placed into ``:processing`` are NEVER rewritten — they stay exactly as
LMOVE deposited them.  This means:

* If a worker crashes between ``reserve`` and ``ack``/``requeue``/``dead_letter``,
  the original bytes are still in ``:processing`` and can be recovered without loss.
* ``ack``/``requeue``/``dead_letter`` remove the entry via
  ``LREM :processing 1 <exact-original-bytes>`` — the match is guaranteed because we
  stored the raw bytes in ``self._inflight[msg.id]`` at reserve time.

In-memory attempt tracking
--------------------------
``reserve()`` returns a ``Message`` whose ``attempts`` field is the decoded value
**plus one** (so callers see delivery count 1, 2, 3 …).  The bytes sitting in
``:processing`` still carry the *original* attempts count (0 on first delivery,
1 after first requeue, etc.).  When ``requeue()`` pushes the message back to the
main queue it serialises the *current* (incremented) attempts so the next
``reserve`` starts from the right baseline.

Move command: ``LMOVE <src> <dst> LEFT RIGHT``
  - Moves the leftmost item of ``<src>`` to the rightmost position of ``<dst>``.
  - Atomic — no race condition between pop and push.
  - Supported by Redis >= 6.2 and by ``fakeredis`` (verified experimentally).

JSON schema stored in each list entry::

    {
        "id":       "<uuid4>",
        "queue":    "<queue name>",
        "payload":  { ... caller dict ... },
        "attempts": <int>
    }

Dead-letter entries extend the schema with ``"error": "<str>"``.

Stale-processing reaper (future work): the processing list makes recovery possible.
A future reaper job would: iterate ``:processing``, parse timestamps/heartbeats,
and LREM + RPUSH any entry older than a visibility timeout back to the main queue.

Typed exceptions
----------------
Every Redis I/O call is wrapped in a ``try/except RedisError`` block.  Raw
``redis.exceptions.RedisError`` never propagates to callers; it is re-raised as
``QueueError`` (a ``ScrapeForgeError`` subclass).

Protocol typing
---------------
The ``_RedisClient`` runtime-checkable Protocol captures the subset of Redis methods
used here (``rpush``, ``lmove``, ``lrem``, ``llen``, ``lrange``).  It lets type
checkers catch typos in method names without coupling to the concrete client class.
"""

from __future__ import annotations

import json
import uuid
from typing import Protocol, runtime_checkable

import redis.exceptions

from scrapeforge.core.queue.base import Message, MessageQueue, QueueError


@runtime_checkable
class _RedisClient(Protocol):
    """Minimal async Redis interface used by RedisQueue."""

    async def rpush(self, name: str, *values: str | bytes) -> int: ...

    async def lmove(
        self, first: str, second: str, src: str = "LEFT", dest: str = "RIGHT"
    ) -> bytes | str | None: ...

    async def lrem(self, name: str, count: int, value: str | bytes) -> int: ...

    async def llen(self, name: str) -> int: ...

    async def lrange(self, name: str, start: int, end: int) -> list[bytes]: ...


class RedisQueue(MessageQueue):
    """Reliable-queue MessageQueue backed by Redis.

    Parameters
    ----------
    redis_client:
        An async Redis client satisfying ``_RedisClient`` (``redis.asyncio.Redis``
        or ``fakeredis.aioredis.FakeRedis``).  The caller owns the lifecycle.
    dlq_suffix:
        Suffix appended to a queue name to form the dead-letter queue name.
        Default ``":dlq"``.
    """

    def __init__(self, redis_client: _RedisClient, *, dlq_suffix: str = ":dlq") -> None:
        super().__init__(dlq_suffix=dlq_suffix)
        self._redis = redis_client
        # Maps message id -> the EXACT raw bytes that LMOVE deposited in :processing.
        # Used by ack/requeue/dead_letter so LREM always matches.
        self._inflight: dict[str, str | bytes] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _processing_key(queue: str) -> str:
        return f"{queue}:processing"

    @staticmethod
    def _encode(msg: Message) -> str:
        """Serialise a Message to a deterministic JSON string.

        ``sort_keys=True`` and ``ensure_ascii=False`` guarantee the same bytes
        regardless of Python dict insertion order, so re-encoded messages match
        stored bytes.
        """
        return json.dumps(
            {
                "id": msg.id,
                "queue": msg.queue,
                "payload": msg.payload,
                "attempts": msg.attempts,
            },
            sort_keys=True,
            ensure_ascii=False,
        )

    @staticmethod
    def _decode(raw: bytes | str) -> Message:
        data = json.loads(raw)
        return Message(
            id=data["id"],
            queue=data["queue"],
            payload=data["payload"],
            attempts=data["attempts"],
        )

    # ------------------------------------------------------------------
    # MessageQueue abstract method implementations
    # ------------------------------------------------------------------

    async def publish(self, queue: str, payload: dict) -> str:
        """Enqueue *payload* onto *queue*; return the assigned message id.

        Builds a ``Message`` with a fresh UUID and ``attempts=0``, serialises it as
        JSON, and appends it to the right end of the Redis list ``<queue>``
        (``RPUSH``).  ``reserve`` pops from the left, preserving FIFO order.

        Raises
        ------
        QueueError
            On any ``redis.exceptions.RedisError``.
        """
        msg_id = str(uuid.uuid4())
        msg = Message(id=msg_id, queue=queue, payload=payload, attempts=0)
        try:
            await self._redis.rpush(queue, self._encode(msg))
        except redis.exceptions.RedisError as exc:
            raise QueueError(f"publish failed on queue {queue!r}: {exc}") from exc
        return msg_id

    async def reserve(self, queue: str) -> Message | None:
        """Atomically move one item from *queue* to ``<queue>:processing``.

        Uses a single ``LMOVE <queue> <queue>:processing LEFT RIGHT`` — atomic in
        Redis, so a crash between this call and ``ack``/``requeue``/``dead_letter``
        leaves the original bytes intact in ``:processing`` (recoverable by a reaper).

        The raw bytes returned by LMOVE are stored in ``self._inflight[msg.id]``
        exactly as-is so that ``LREM`` in ``ack``/``requeue``/``dead_letter`` can
        match them byte-for-byte.  The processing list entry is NEVER rewritten.

        Returns a ``Message`` whose ``attempts`` is the decoded value **plus one**
        (delivery count 1, 2, 3 …) — this increment lives only in memory.

        Returns ``None`` if the main queue is empty.

        Raises
        ------
        QueueError
            On any ``redis.exceptions.RedisError``.
        """
        proc_key = self._processing_key(queue)
        try:
            raw = await self._redis.lmove(queue, proc_key, "LEFT", "RIGHT")
        except redis.exceptions.RedisError as exc:
            raise QueueError(f"reserve failed on queue {queue!r}: {exc}") from exc
        if raw is None:
            return None

        # Decode and bump attempts in memory only — do NOT rewrite :processing.
        msg = self._decode(raw)
        delivered = Message(
            id=msg.id,
            queue=msg.queue,
            payload=msg.payload,
            attempts=msg.attempts + 1,
        )
        # Remember the EXACT bytes so ack/requeue/dead_letter can LREM them.
        self._inflight[delivered.id] = raw
        return delivered

    async def ack(self, message: Message) -> None:
        """Remove *message* from the processing list (mark done).

        Uses ``LREM <queue>:processing 1 <original-raw-bytes>`` where the raw bytes
        are retrieved from ``self._inflight`` — guaranteed to match what LMOVE put
        there, regardless of key ordering in the payload.

        Raises
        ------
        QueueError
            On any ``redis.exceptions.RedisError``.
        """
        proc_key = self._processing_key(message.queue)
        raw = self._inflight.pop(message.id, self._encode(message))
        try:
            await self._redis.lrem(proc_key, 1, raw)
        except redis.exceptions.RedisError as exc:
            raise QueueError(f"ack failed for message {message.id!r}: {exc}") from exc

    async def requeue(self, message: Message) -> None:
        """Return *message* to the tail of its queue for another attempt.

        Removes from the processing list using the stored original bytes, then
        pushes the message (with its current in-memory ``attempts`` count) back
        to the main queue.  The next ``reserve`` will increment ``attempts`` again,
        so the next delivery sees the correct cumulative count.

        Raises
        ------
        QueueError
            On any ``redis.exceptions.RedisError``.
        """
        proc_key = self._processing_key(message.queue)
        inflight_raw = self._inflight.pop(message.id, self._encode(message))
        try:
            await self._redis.lrem(proc_key, 1, inflight_raw)
            await self._redis.rpush(message.queue, self._encode(message))
        except redis.exceptions.RedisError as exc:
            raise QueueError(f"requeue failed for message {message.id!r}: {exc}") from exc

    async def dead_letter(self, message: Message, error: str) -> None:
        """Move *message* to ``<queue><dlq_suffix>`` with the *error* string.

        Removes from the processing list (exact original bytes) and appends a
        dead-letter entry to ``<queue>:dlq``.  The entry reuses ``_encode``'s
        canonical JSON and adds the ``"error"`` field, keeping the schema
        consistent.

        Raises
        ------
        QueueError
            On any ``redis.exceptions.RedisError``.
        """
        proc_key = self._processing_key(message.queue)
        dlq_key = f"{message.queue}{self._dlq_suffix}"
        inflight_raw = self._inflight.pop(message.id, self._encode(message))

        # Build DLQ entry by extending the canonical encoded dict.
        base = json.loads(self._encode(message))
        base["error"] = error
        dlq_entry = json.dumps(base, sort_keys=True, ensure_ascii=False)

        try:
            await self._redis.lrem(proc_key, 1, inflight_raw)
            await self._redis.rpush(dlq_key, dlq_entry)
        except redis.exceptions.RedisError as exc:
            raise QueueError(f"dead_letter failed for message {message.id!r}: {exc}") from exc

    async def size(self, queue: str) -> int:
        """Return the number of *waiting* (not in-flight) messages on *queue*.

        Queries ``LLEN <queue>`` — the main list only.  In-flight messages in
        ``<queue>:processing`` are intentionally excluded.

        Raises
        ------
        QueueError
            On any ``redis.exceptions.RedisError``.
        """
        try:
            return await self._redis.llen(queue)
        except redis.exceptions.RedisError as exc:
            raise QueueError(f"size failed on queue {queue!r}: {exc}") from exc

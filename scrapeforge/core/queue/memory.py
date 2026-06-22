"""InMemoryMessageQueue — first-class in-process implementation of the MessageQueue port.

Backed by ``collections.deque`` per queue name, a dict for dead-letter lists, and an
in-flight registry keyed by message id.  Thread-safety is NOT required because this
runs on a single asyncio event loop.

This is NOT test-only infrastructure — it is a first-class implementation usable for
local / single-process runs and as a drop-in test double for higher-level tests.
"""

from __future__ import annotations

import uuid
from collections import defaultdict, deque

from scrapeforge.core.queue.base import Message, MessageQueue


class InMemoryMessageQueue(MessageQueue):
    """A fully working in-process MessageQueue backed by Python deques.

    Parameters
    ----------
    dlq_suffix:
        Suffix appended to a queue name to form its dead-letter queue name.
        Defaults to ``":dlq"``.
    """

    def __init__(self, *, dlq_suffix: str = ":dlq") -> None:
        super().__init__(dlq_suffix=dlq_suffix)
        # queue_name → deque[Message]  (waiting messages, FIFO)
        self._queues: dict[str, deque[Message]] = defaultdict(deque)
        # message_id → Message  (in-flight registry)
        self._inflight: dict[str, Message] = {}
        # queue_name → list[Message]  (dead-letter store, without the suffix key)
        self._dead_letters: dict[str, list[Message]] = defaultdict(list)

    # ------------------------------------------------------------------
    # MessageQueue abstract method implementations
    # ------------------------------------------------------------------

    async def publish(self, queue: str, payload: dict) -> str:
        """Append *payload* to *queue*; return a new unique message id.

        Payloads are stored by reference (no copy).  Callers must treat them as
        immutable after publish — see the ABC docstring for the contract.
        """
        msg_id = str(uuid.uuid4())
        self._queues[queue].append(Message(id=msg_id, queue=queue, payload=payload))
        return msg_id

    async def reserve(self, queue: str) -> Message | None:
        """Pop the leftmost (oldest) message, increment its ``attempts``, track in-flight."""
        if not self._queues[queue]:
            return None
        msg = self._queues[queue].popleft()
        # Increment attempts.  Message uses slots so we must reconstruct it.
        msg = Message(id=msg.id, queue=msg.queue, payload=msg.payload, attempts=msg.attempts + 1)
        self._inflight[msg.id] = msg
        return msg

    async def ack(self, message: Message) -> None:
        """Remove *message* from the in-flight registry (mark done)."""
        self._inflight.pop(message.id, None)

    async def requeue(self, message: Message) -> None:
        """Append *message* back to the tail of its queue (attempts preserved)."""
        self._inflight.pop(message.id, None)
        self._queues[message.queue].append(message)

    async def dead_letter(self, message: Message, error: str) -> None:  # noqa: ARG002
        """Move *message* to the dead-letter store for its queue."""
        self._inflight.pop(message.id, None)
        self._dead_letters[message.queue].append(message)

    async def size(self, queue: str) -> int:
        """Return the number of waiting (not in-flight) messages on *queue*."""
        return len(self._queues[queue])

    # ------------------------------------------------------------------
    # Inspection helper (not part of the abstract port)
    # ------------------------------------------------------------------

    def dead_letters(self, queue: str) -> list[Message]:
        """Return the list of dead-lettered messages for *queue*.

        This is an inspection / observability method for tests and monitoring.
        It does NOT include the DLQ suffix — pass the original queue name.
        """
        return list(self._dead_letters[queue])

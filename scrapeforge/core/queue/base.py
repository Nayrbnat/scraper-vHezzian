"""MessageQueue port — abstract interface for message queues with retry + DLQ support.

This module defines the contract that all queue backends must implement.
The in-memory implementation lives in ``scrapeforge.core.queue.memory``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from scrapeforge.exceptions import ScrapeForgeError


class QueueError(ScrapeForgeError):
    """Queue-level failure: publish, reserve, ack, requeue, or dead-letter error."""


@dataclass(slots=True)
class Message:
    """Immutable-enough value object representing one queue message."""

    id: str
    queue: str
    payload: dict
    attempts: int = 0


class MessageQueue(ABC):
    """Abstract base class (port) for all message-queue backends.

    Concrete implementations back this with Redis, in-memory, etc.
    The ``consume_once`` helper is a CONCRETE method on this base — it composes the
    abstract primitives so callers never need to wire the retry / DLQ logic themselves.
    """

    def __init__(self, *, dlq_suffix: str = ":dlq") -> None:
        self._dlq_suffix = dlq_suffix

    # ------------------------------------------------------------------
    # Abstract primitives — every backend must implement these.
    # ------------------------------------------------------------------

    @abstractmethod
    async def publish(self, queue: str, payload: dict) -> str:
        """Enqueue *payload* onto *queue*; return the assigned message id.

        Note: payloads are caller-owned and must be treated as immutable by handlers.
        Backends are not required to deep-copy on publish or reserve.
        """

    @abstractmethod
    async def reserve(self, queue: str) -> Message | None:
        """Pop the next waiting message FIFO, increment its ``attempts``, and track it
        as in-flight.  Return ``None`` if the queue is empty."""

    @abstractmethod
    async def ack(self, message: Message) -> None:
        """Mark *message* as successfully processed and remove it from in-flight."""

    @abstractmethod
    async def requeue(self, message: Message) -> None:
        """Return a reserved message to the tail of its queue for another attempt.
        The ``message.attempts`` value is preserved (not re-incremented here)."""

    @abstractmethod
    async def dead_letter(self, message: Message, error: str) -> None:
        """Move *message* to the dead-letter queue ``f"{message.queue}{dlq_suffix}"``,
        recording *error* for inspection.  The message is removed from in-flight."""

    @abstractmethod
    async def size(self, queue: str) -> int:
        """Return the number of *waiting* (not in-flight) messages on *queue*."""

    # ------------------------------------------------------------------
    # Concrete helper — composes the abstract primitives.
    # ------------------------------------------------------------------

    async def consume_once(
        self,
        queue: str,
        handler: Callable[[dict], Awaitable[None]],
        *,
        max_retries: int,
    ) -> bool:
        """Reserve one message from *queue* and drive it through *handler*.

        Returns
        -------
        True
            A message was found and processed (acked, requeued, or dead-lettered).
        False
            The queue was empty; *handler* was not called.

        Retry / DLQ logic
        -----------------
        * Success  → ``ack(message)``
        * Handler raises and ``message.attempts <= max_retries`` → ``requeue(message)``
        * Handler raises and ``message.attempts > max_retries``
          → ``dead_letter(message, str(exc))``

        The handler runs at most ``max_retries + 1`` times before the message is
        dead-lettered.  ``max_retries=0`` means no retries: one attempt, then DLQ.
        """
        message = await self.reserve(queue)
        if message is None:
            return False

        try:
            await handler(message.payload)
        except Exception as exc:  # noqa: BLE001
            if message.attempts > max_retries:
                await self.dead_letter(message, str(exc))
            else:
                await self.requeue(message)
        else:
            await self.ack(message)

        return True

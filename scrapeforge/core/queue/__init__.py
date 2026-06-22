"""Queue port — abstract message-queue interface and in-memory implementation.

Exports the public surface so callers can do:
    from scrapeforge.core.queue import InMemoryMessageQueue, Message, MessageQueue, QueueError
"""

from scrapeforge.core.queue.base import Message, MessageQueue, QueueError
from scrapeforge.core.queue.memory import InMemoryMessageQueue

__all__ = ["Message", "MessageQueue", "QueueError", "InMemoryMessageQueue"]

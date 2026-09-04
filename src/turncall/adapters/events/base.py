"""Abstract event bus adapter interface."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Event:
    """An event to be published."""

    event_type: str
    payload: dict[str, Any]
    event_id: str | None = None
    timestamp: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class EventBusAdapter(ABC):
    """Interface for event bus implementations.

    Starts with webhook-based delivery.
    Future: Kafka, Redis Streams, etc.
    """

    @abstractmethod
    async def publish(self, topic: str, event: Event) -> None:
        """Publish an event to a topic."""

    @abstractmethod
    async def subscribe(
        self,
        topic: str,
        handler: Callable[[Event], Any],
        *,
        group: str | None = None,
    ) -> None:
        """Subscribe to events on a topic."""

    @abstractmethod
    async def stream(
        self,
        topic: str,
        *,
        group: str | None = None,
        from_beginning: bool = False,
    ) -> AsyncIterator[Event]:
        """Stream events from a topic."""
        yield  # type: ignore[misc]

    @abstractmethod
    async def start(self) -> None:
        """Start the event bus (connect, create topics, etc.)."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the event bus and clean up."""

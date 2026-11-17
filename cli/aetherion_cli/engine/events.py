"""Unified event model and bounded broadcast bus.

The engine, TUI, transcript writer, and tests communicate through this module.
The bus deliberately uses bounded per-subscriber queues: a slow consumer must
produce an explicit ``asyncio.QueueFull`` failure rather than silently growing
memory or losing semantic events.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """All Phase 0/1 events in the unified Aetherion event protocol."""

    STATE_CHANGED = "state_changed"
    USER_INPUT = "user_input"
    PLAN_READY = "plan_ready"
    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    TOOL_DETECTED = "tool_detected"
    TOOL_ARGS_DELTA = "tool_args_delta"
    TOOL_CALL = "tool_call"
    PERMISSION_REQUEST = "permission_request"
    PERMISSION_RESOLVED = "permission_resolved"
    TOOL_STARTED = "tool_started"
    TOOL_PROGRESS = "tool_progress"
    TOOL_FINISHED = "tool_finished"
    TOOL_FAILED = "tool_failed"
    CONTEXT_COMPACTING = "context_compacting"
    CONTEXT_COMPACTED = "context_compacted"
    TASK_STARTED = "task_started"
    TASK_FINISHED = "task_finished"
    TASK_FAILED = "task_failed"
    TURN_FINISHED = "turn_finished"
    TURN_CANCELLED = "turn_cancelled"
    ERROR = "error"


@dataclass(slots=True, frozen=True)
class Event:
    """Immutable event envelope; nested ``data`` is treated as read-only."""

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    step: int = 0
    turn_id: str = ""

    def copy(self) -> Event:
        """Return a defensive event copy for consumers that mutate payloads."""

        return Event(self.type, dict(self.data), self.step, self.turn_id)


class EventSubscription(AsyncIterator[Event]):
    """One independent bounded view of an :class:`EventBus`."""

    def __init__(self, bus: EventBus, token: int, queue: asyncio.Queue[Event]) -> None:
        self._bus = bus
        self._token = token
        self._queue = queue
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def next(self) -> Event:
        """Wait for the next event delivered to this subscriber."""

        if self._closed:
            raise RuntimeError("event subscription is closed")
        return await self._queue.get()

    async def __anext__(self) -> Event:
        try:
            return await self.next()
        except RuntimeError as exc:
            raise StopAsyncIteration from exc

    def __aiter__(self) -> EventSubscription:
        return self

    def close(self) -> None:
        """Unsubscribe without affecting other consumers."""

        if not self._closed:
            self._closed = True
            self._bus.unsubscribe(self)


class EventBus:
    """Bounded broadcast event bus with a backwards-compatible ``next`` API.

    ``next()`` reads from the default subscription. Consumers that need every
    event independently should call ``subscribe()`` and retain the returned
    subscription. ``publish`` is synchronous by design; if any active queue is
    full it raises ``asyncio.QueueFull`` before placing the event in any queue.
    """

    def __init__(self, maxsize: int = 4096) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be positive")
        self.maxsize = maxsize
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._subscribers: dict[int, asyncio.Queue[Event]] = {0: self.queue}
        self._subscriptions: dict[int, EventSubscription] = {}
        self._next_token = 1
        self.closed = False

    def subscribe(self) -> EventSubscription:
        """Create a bounded subscriber that receives every future event."""

        if self.closed:
            raise RuntimeError("event bus is closed")
        token = self._next_token
        self._next_token += 1
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self.maxsize)
        self._subscribers[token] = queue
        subscription = EventSubscription(self, token, queue)
        self._subscriptions[token] = subscription
        return subscription

    def unsubscribe(self, subscription: EventSubscription) -> None:
        """Remove a subscription; closing an unknown subscription is harmless."""

        self._subscribers.pop(subscription._token, None)
        self._subscriptions.pop(subscription._token, None)

    def publish(self, event: Event) -> None:
        """Broadcast an event or raise ``QueueFull`` without partial delivery."""

        if self.closed:
            return
        queues = tuple(self._subscribers.values())
        if any(queue.full() for queue in queues):
            raise asyncio.QueueFull
        for queue in queues:
            queue.put_nowait(event)

    async def next(self) -> Event:
        """Read from the default subscription for simple consumers."""

        if self.closed and self.queue.empty():
            raise RuntimeError("event bus is closed")
        return await self.queue.get()

    def close(self) -> None:
        """Stop accepting events and close all explicit subscriptions."""

        if self.closed:
            return
        self.closed = True
        for subscription in tuple(self._subscriptions.values()):
            subscription._closed = True
        self._subscriptions.clear()
        self._subscribers.clear()

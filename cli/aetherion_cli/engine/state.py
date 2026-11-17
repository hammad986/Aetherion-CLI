"""Strict engine state machine for control-plane lifecycle transitions."""

from __future__ import annotations

from enum import Enum


class EngineState(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    STREAMING = "streaming"
    TOOL_PENDING = "tool_pending"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    OBSERVING = "observing"
    COMPACTING = "compacting"
    DONE = "done"
    CANCELLED = "cancelled"
    ERROR = "error"


_TERMINAL = frozenset({EngineState.DONE, EngineState.CANCELLED, EngineState.ERROR})
_ACTIVE = frozenset(set(EngineState) - _TERMINAL)


# Fatal/cancel transitions are deliberately present for every active state.
# This makes cancellation and error reporting reliable even while a provider,
# tool, or compactor is awaiting an external future.
ALLOWED: dict[EngineState, set[EngineState]] = {
    EngineState.IDLE: {EngineState.PLANNING, EngineState.STREAMING, EngineState.CANCELLED, EngineState.ERROR},
    EngineState.PLANNING: {EngineState.STREAMING, EngineState.IDLE, EngineState.CANCELLED, EngineState.ERROR},
    EngineState.STREAMING: {EngineState.TOOL_PENDING, EngineState.COMPACTING, EngineState.DONE, EngineState.CANCELLED, EngineState.ERROR},
    EngineState.TOOL_PENDING: {EngineState.EXECUTING, EngineState.AWAITING_APPROVAL, EngineState.OBSERVING, EngineState.CANCELLED, EngineState.ERROR},
    EngineState.AWAITING_APPROVAL: {EngineState.EXECUTING, EngineState.OBSERVING, EngineState.CANCELLED, EngineState.ERROR},
    EngineState.EXECUTING: {EngineState.OBSERVING, EngineState.CANCELLED, EngineState.ERROR},
    EngineState.OBSERVING: {EngineState.STREAMING, EngineState.DONE, EngineState.CANCELLED, EngineState.ERROR},
    EngineState.COMPACTING: {EngineState.STREAMING, EngineState.ERROR, EngineState.CANCELLED},
    EngineState.DONE: {EngineState.IDLE},
    EngineState.CANCELLED: {EngineState.IDLE},
    EngineState.ERROR: {EngineState.IDLE},
}


class InvalidTransition(RuntimeError):
    """Raised when an engine attempts a state change outside ``ALLOWED``."""

    def __init__(self, current: EngineState, nxt: EngineState) -> None:
        super().__init__(f"illegal engine transition {current.value} -> {nxt.value}")
        self.current = current
        self.next_state = nxt


def transition(current: EngineState, nxt: EngineState) -> EngineState:
    """Validate and return ``nxt``.

    Re-emitting the current state is harmless for callers that model streaming
    deltas, while every distinct transition is checked against the matrix.
    """

    if current == nxt:
        return current
    if nxt not in ALLOWED[current]:
        raise InvalidTransition(current, nxt)
    return nxt

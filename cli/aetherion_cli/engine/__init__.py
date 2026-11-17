"""Core asynchronous engine primitives for Aetherion CLI."""

from .budget import BudgetSlices, TokenBudget, UsageSnapshot
from .events import Event, EventBus, EventSubscription, EventType
from .loop import (
    AetherionConfig,
    Capabilities,
    Config,
    DoneEvent,
    Engine,
    ModelEvent,
    ModelRequest,
    ReasoningDelta,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolResult,
    UsageEvent,
)
from .state import ALLOWED, EngineState, InvalidTransition, transition

__all__ = [
    "ALLOWED",
    "AetherionConfig",
    "Capabilities",
    "Config",
    "DoneEvent",
    "Engine",
    "EngineState",
    "Event",
    "EventBus",
    "EventSubscription",
    "EventType",
    "InvalidTransition",
    "ModelEvent",
    "ModelRequest",
    "ReasoningDelta",
    "TextDelta",
    "TokenBudget",
    "ToolCall",
    "ToolCallEvent",
    "ToolResult",
    "UsageEvent",
    "UsageSnapshot",
    "BudgetSlices",
    "transition",
]

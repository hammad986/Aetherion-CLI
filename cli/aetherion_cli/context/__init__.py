"""Conversation context, compaction, and transcript persistence."""

from .compact import (
    CompactionResult,
    flatten_groups,
    full_compact,
    group_messages,
    micro_compact,
    reactive_compact,
    summarize_messages,
)
from .manager import ContextManager, ContextSnapshot, ContextValue, TranscriptSink
from .transcript import BranchLoad, Transcript, TranscriptRecord, load_branch

__all__ = [
    "BranchLoad",
    "CompactionResult",
    "ContextManager",
    "ContextSnapshot",
    "ContextValue",
    "Transcript",
    "TranscriptRecord",
    "TranscriptSink",
    "flatten_groups",
    "full_compact",
    "group_messages",
    "load_branch",
    "micro_compact",
    "reactive_compact",
    "summarize_messages",
]

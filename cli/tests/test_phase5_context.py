"""Focused Phase 5 context assembly, eviction, and compaction tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

from aetherion_cli.context.compact import micro_compact
from aetherion_cli.context.manager import ContextManager
from aetherion_cli.context.transcript import Transcript
from aetherion_cli.engine.budget import estimate_tokens
from aetherion_cli.engine.loop import Capabilities, ToolCall, ToolResult


def test_request_assembly_is_ordered_and_reserves_output_tokens() -> None:
    manager = ContextManager(
        system="system instructions",
        environment="environment facts",
        repo_map="repository map",
        memory="remembered fact",
        summary="prior summary",
        context_window=40_000,
    )
    manager.add_user("current turn")

    request = manager.build_request(Capabilities(max_context=40_000))

    positions = [
        request.system.index("[system]"),
        request.system.index("[environment]"),
        request.system.index("[repo_map]"),
        request.system.index("[memory]"),
        request.system.index("[summary]"),
    ]
    assert positions == sorted(positions)
    assert request.messages[-1] == {"role": "user", "content": "current turn"}
    assert request.max_tokens == 16_384
    assert estimate_tokens(request.messages, request.tools, request.system) <= 40_000 - 16_384


def test_tool_call_and_result_are_evicted_as_one_atomic_group() -> None:
    messages = [
        {"role": "user", "content": "old request"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "call-1", "name": "read", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "old output"}],
        },
        {"role": "user", "content": "new request"},
    ]

    result = micro_compact(messages, keep_groups=1)

    assert result.retained_messages == [{"role": "user", "content": "new request"}]
    assert result.evicted_messages[1]["role"] == "assistant"
    assert result.evicted_messages[2]["role"] == "user"
    assert all(
        block.get("type") not in {"tool_use", "tool_result"}
        for message in result.retained_messages
        for block in message.get("content", [])
        if isinstance(block, dict)
    )


def test_needs_compaction_runs_tier_one_before_request_build() -> None:
    manager = ContextManager(context_window=60_000, recent_message_count=12)
    for index in range(14):
        manager.add_user(f"user-{index} " + ("u" * 5_000))
        manager.add_assistant(f"assistant-{index} " + ("a" * 5_000), ())

    assert manager.needs_compaction() is False
    assert manager.last_mode == "micro"
    assert manager.last_compaction is not None
    assert manager.last_compaction.evicted_messages


def test_full_and_reactive_compaction_pin_goal_and_write_boundaries(tmp_path: Path) -> None:
    transcript = Transcript(tmp_path / "context.jsonl", cwd=str(tmp_path))
    manager = ContextManager(
        transcript,
        pinned_goal="ship the context manager",
        recent_message_count=1,
    )
    manager.add_user("old user request")
    manager.add_assistant("old answer", ())
    manager.add_user("latest request")

    asyncio.run(manager.compact(mode="full"))

    assert manager.last_mode == "full"
    assert "Pinned goal: ship the context manager" in manager.summary
    assert any(record.get("subtype") == "compact_boundary" for record in transcript.branch)

    manager.add_user("reactive request")
    manager.add_assistant(
        "tool step",
        [ToolCall("call-2", "read", {})],
    )
    manager.add_tool_results([ToolResult("call-2", True, "output")])
    asyncio.run(manager.compact(mode="reactive"))

    assert manager.last_mode == "reactive"
    assert "Pinned goal: ship the context manager" in manager.summary
    assert any(record.get("compactMetadata", {}).get("trigger") == "reactive" for record in transcript.branch)

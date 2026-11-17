"""Focused Phase 2/3/4 parser, tools, security, and diff tests."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from aetherion_cli.diff.fuzzy import FuzzyMatchError, replace_anchor
from aetherion_cli.diff.patch import PatchError, apply_file_edit
from aetherion_cli.engine.events import EventBus
from aetherion_cli.engine.loop import ToolCall
from aetherion_cli.parsers.hermes import HermesStreamParser
from aetherion_cli.providers.anthropic import NativeContentAccumulator
from aetherion_cli.providers.hermes import HermesProvider
from aetherion_cli.providers.openai import ResponseToolAssembler
from aetherion_cli.security.paths import PathPolicy, PathSecurityError, WorkspaceRoots
from aetherion_cli.security.permissions import (
    PermissionEffect,
    PermissionGate,
    PermissionMode,
    PermissionRule,
)
from aetherion_cli.security.risk import RiskTier, classify_command
from aetherion_cli.tools.dispatch import ToolDispatcher
from aetherion_cli.tools.registry import ToolRegistry
from aetherion_cli.tools.shell import CommandRunner


def test_hermes_parser_handles_every_chunk_boundary_and_nested_json() -> None:
    source = '<scratchpad>think</scratchpad>before<tool_call>{"name":"read_file","arguments":{"path":"a</tool_call>b","items":[1,{"x":true}]}}</tool_call>after'
    parser = HermesStreamParser(turn_id="turn-1")
    events = []
    for character in source:
        events.extend(parser.feed(character))
    events.extend(parser.finish())
    calls = [event.call for event in events if hasattr(event, "call")]
    text = "".join(event.text for event in events if event.__class__.__name__ == "TextDelta")
    reasoning = "".join(event.text for event in events if event.__class__.__name__ == "ReasoningDelta")
    assert len(calls) == 1
    assert calls[0].call_id == "turn-1:tool:0"
    assert calls[0].arguments["path"] == "a</tool_call>b"
    assert text == "beforeafter"
    assert reasoning == "think"


def test_hermes_parser_repairs_bounded_trailing_comma_and_preserves_metadata() -> None:
    parser = HermesStreamParser(turn_id="repair")
    events = parser.feed('<tool_call>{"name":"x","arguments":{"a":1,}}</tool_call>') + parser.finish()
    call = next(event.call for event in events if hasattr(event, "call"))
    assert call.arguments == {"a": 1}
    assert call.metadata["repaired"] is True
    assert "trailing_comma" in call.metadata["repairs"]


@pytest.mark.asyncio
async def test_hermes_provider_bridge_and_native_provider_assemblers() -> None:
    class Client:
        async def stream(self, _messages):
            yield '<tool_call>{"name":"read_file","arguments":{"path":"x"}}</tool_call>'

    provider = HermesProvider(Client())
    request = type("Request", (), {"messages": [], "system": "", "turn_id": "turn", "max_tokens": 10, "temperature": 0.2})()
    events = [event async for event in provider.stream(request, cancel=asyncio.Event())]
    assert any(getattr(event, "call", None) is not None for event in events)

    assembler = ResponseToolAssembler()
    assembler.add_call("call-1", "read_file", '{"path":"x"}')
    assembler.append_arguments("call-1", "")
    assert assembler.complete_call("call-1").arguments == {"path": "x"}
    with pytest.raises(ValueError, match="duplicate"):
        assembler.complete_call("call-1")

    native = NativeContentAccumulator()
    native.start(0, {"type": "tool_use", "id": "tool-1", "name": "read_file"})
    native.delta(0, {"type": "input_json_delta", "partial_json": '{"path":"x"}'})
    assert native.stop(0).arguments == {"path": "x"}


class _Args(BaseModel):
    path: str


@pytest.mark.asyncio
async def test_registry_strict_schema_and_dispatch_model_order() -> None:
    registry = ToolRegistry()
    order: list[str] = []

    async def read(data, _ctx):
        await asyncio.sleep(0.01)
        order.append(f"read:{data.path}")
        return data.path

    async def write(data, _ctx):
        order.append(f"write:{data.path}")
        return data.path

    registry.register("read_file", "read", _Args, read, is_read_only=True, is_concurrency_safe=True, risk_tier=1)
    registry.register("write_file", "write", _Args, write, is_read_only=False, is_concurrency_safe=False, risk_tier=2)
    schema = registry.get("read_file").strict_schema()
    assert schema["additionalProperties"] is False
    with pytest.raises(ValueError, match="unknown tool argument"):
        registry.parse_arguments(registry.get("read_file"), {"path": "x", "extra": 1})

    engine = SimpleNamespace(cancel=asyncio.Event(), bus=EventBus(), current_turn_id="turn")
    dispatcher = ToolDispatcher(registry, permissions=PermissionGate(mode=PermissionMode.BYPASS))
    results = await dispatcher.run(
        [
            ToolCall("r1", "read_file", {"path": "one"}),
            ToolCall("w1", "write_file", {"path": "two"}),
            ToolCall("r2", "read_file", {"path": "three"}),
        ],
        engine,
        1,
    )
    assert [result.call_id for result in results] == ["r1", "w1", "r2"]
    assert order[-1] == "write:two"


def test_permission_deny_beats_allow_and_risk_is_explainable() -> None:
    gate = PermissionGate(
        mode=PermissionMode.DEFAULT,
        rules=[
            PermissionRule("bash", PermissionEffect.ALLOW, priority=10),
            PermissionRule("bash", PermissionEffect.DENY, priority=20),
        ],
    )
    decision = asyncio.run(gate.check(ToolCall("1", "bash", {"command": "echo ok"}), None))
    assert decision.effect is PermissionEffect.DENY
    assert classify_command("git reset --hard HEAD").tier is RiskTier.TIER_3


def test_paths_reject_traversal_and_allow_workspace_files(tmp_path: Path) -> None:
    policy = PathPolicy(WorkspaceRoots(tmp_path))
    safe = policy.assert_writable("nested/file.txt")
    assert safe.parent == (tmp_path / "nested").resolve()
    with pytest.raises(PathSecurityError) as exc_info:
        policy.assert_readable("../outside.txt")
    assert exc_info.value.code in {"path_not_canonical", "not_found"}


def test_paths_reject_missing_child_below_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    policy = PathPolicy(WorkspaceRoots(tmp_path))
    with pytest.raises(PathSecurityError) as exc_info:
        policy.assert_writable("link/new.txt")
    assert exc_info.value.code == "symlink_in_path"


def test_fuzzy_ladder_and_ambiguity() -> None:
    result = replace_anchor("def f():\n    return 1\n", "def f():\nreturn 1\n", "def f():\n    return 2\n")
    assert result.text.endswith("return 2\n")
    with pytest.raises(FuzzyMatchError, match="ambiguous"):
        replace_anchor("x\nx\n", "x", "y")


def test_atomic_diff_edit_rejects_stale_hash_and_preserves_file(tmp_path: Path) -> None:
    path = tmp_path / "file.py"
    path.write_text("value = 1\n", encoding="utf-8")
    with pytest.raises(PatchError, match="stale"):
        apply_file_edit(path, "value = 1", "value = 2", expected_sha256="0" * 64)
    assert path.read_text(encoding="utf-8") == "value = 1\n"
    patch = apply_file_edit(path, "value = 1", "value = 2")
    assert "+value = 2" in patch.diff
    assert path.read_text(encoding="utf-8") == "value = 2\n"


@pytest.mark.asyncio
async def test_argv_command_runner_executes_without_shell() -> None:
    runner = CommandRunner()
    result = await runner.run_argv([sys.executable, "-c", "print('ok')"])
    assert result.ok is True
    assert result.output["stdout"].strip() == "ok"

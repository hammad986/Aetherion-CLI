"""Local Engine construction and UI/CLI event bridging.

The local runtime is deliberately dependency-injected.  Vendor clients may be
passed by an application, while the built-in provider is deterministic and
offline-safe for headless use and tests.  Plugin and skill catalogues are
exposed as metadata only; their code is never imported or executed here.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..context import ContextManager, Transcript
from ..engine.events import Event, EventBus
from ..engine.loop import (
    AetherionConfig,
    Capabilities,
    DoneEvent,
    Engine,
    ModelRequest,
    Provider,
    TextDelta,
)
from ..repo import build_reading_plan, index_repository
from ..security.paths import PathPolicy, WorkspaceRoots
from ..security.permissions import PermissionGate, PermissionMode
from ..tools import (
    CommandRunner,
    FileTools,
    ToolDispatcher,
    ToolRegistry,
    register_file_tools,
    register_patch_tools,
    register_shell_tools,
)


class RepoMapInput(BaseModel):
    query: str = ""
    token_budget: int = Field(default=3_000, ge=2_000, le=4_000)
    max_files: int = Field(default=10, ge=5, le=15)


class IntegrationCatalogInput(BaseModel):
    include_skills: bool = True


class SkillInvokeInput(BaseModel):
    skill_id: str = Field(min_length=1, max_length=128)


class PluginInspectInput(BaseModel):
    plugin_id: str = Field(min_length=1, max_length=128)


class ReaInspectInput(BaseModel):
    args: list[str] = Field(default_factory=list, max_length=32)


@dataclass(slots=True)
class LocalToolContext:
    workspace: Path
    path_policy: PathPolicy
    file_tools: FileTools
    command_runner: CommandRunner
    cancel: asyncio.Event
    on_tool_output: Callable[[str, str], Any] | None = None


class EchoProvider:
    """Offline provider used when no vendor client is explicitly injected."""

    capabilities = Capabilities(native_tools=True, strict_schemas=True, parallel_calls=True, max_context=200_000)

    def __init__(self, *, prefix: str = "Aetherion local") -> None:
        self.prefix = prefix

    async def stream(self, request: ModelRequest, *, cancel: asyncio.Event):
        if cancel.is_set():
            return
        prompt = ""
        for message in reversed(request.messages):
            if message.get("role") == "user":
                content = message.get("content", "")
                prompt = content if isinstance(content, str) else str(content)
                break
        yield TextDelta(f"{self.prefix}: {prompt}".strip())
        yield DoneEvent("stop")


ProviderFactory = Callable[[str, str, Sequence[Mapping[str, Any]]], Provider]
EventHandler = Callable[[Event], Any]


class LocalEngineController:
    """Own one local Engine and bridge its bounded event stream to a caller."""

    def __init__(self, engine: Engine, permissions: PermissionGate, bus: EventBus) -> None:
        self.engine = engine
        self.permissions = permissions
        self.bus = bus
        self.pending_approvals: dict[str, asyncio.Future[str]] = {}
        self._consumer_task: asyncio.Task[None] | None = None
        self._on_event: EventHandler | None = None
        self._consuming_event = False

    async def run_turn(self, prompt: str, on_event: EventHandler | None = None):
        if self._consumer_task is not None and not self._consumer_task.done():
            raise RuntimeError("a local turn is already active")
        self._on_event = on_event
        self._consumer_task = asyncio.create_task(self._consume_events())
        try:
            return await self.engine.run_turn(prompt)
        finally:
            # Engine publication is synchronous, while callbacks may be async.
            # Drain the bounded queue before stopping the consumer so a fast
            # offline provider cannot lose final text/tool events.
            while self.bus.queue.qsize() or self._consuming_event:
                await asyncio.sleep(0)
            if self._consumer_task is not None:
                self._consumer_task.cancel()
                await asyncio.gather(self._consumer_task, return_exceptions=True)
                self._consumer_task = None
            self._on_event = None

    async def _consume_events(self) -> None:
        while True:
            event = await self.bus.next()
            self._consuming_event = True
            try:
                try:
                    if self._on_event is not None:
                        result = self._on_event(event)
                        if asyncio.iscoroutine(result):
                            await result
                except Exception:
                    # Rendering must never stop the event drain or strand a
                    # completed engine turn behind a slow/failing UI callback.
                    pass
            finally:
                self._consuming_event = False

    async def approval_callback(self, _decision: Any, call: Any, _engine: Engine) -> str:
        request_id = f"{self.engine.current_turn_id or 'turn'}:permission:{call.call_id}"
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.pending_approvals[request_id] = future
        try:
            return await future
        finally:
            self.pending_approvals.pop(request_id, None)

    def resolve_approval(self, request_id: str, decision: str) -> None:
        future = self.pending_approvals.get(request_id)
        if future is not None and not future.done():
            future.set_result(decision)

    def cancel(self) -> None:
        self.engine.request_cancel()
        for future in tuple(self.pending_approvals.values()):
            if not future.done():
                future.set_result("deny")


class LocalEngineFactory:
    """Build a fully local engine from a workspace and optional provider."""

    @staticmethod
    def build(
        workspace: str | Path,
        *,
        provider: Provider | None = None,
        provider_name: str = "local",
        model: str = "local",
        provider_factory: ProviderFactory | None = None,
        permission_mode: PermissionMode | str = PermissionMode.DEFAULT,
        approval_callback: Callable[..., Any] | None = None,
        transcript_path: str | Path | None = None,
        system_prompt: str = "You are Aetherion, a careful local coding agent.",
        context_window: int = 200_000,
        max_steps: int = 24,
    ) -> LocalEngineController:
        root = Path(workspace).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"workspace is not a directory: {root}")
        roots = WorkspaceRoots(primary_root=root)
        path_policy = PathPolicy(roots)
        file_tools = FileTools(path_policy)
        command_runner = CommandRunner()
        tool_context = LocalToolContext(root, path_policy, file_tools, command_runner, asyncio.Event())

        registry = ToolRegistry()
        register_file_tools(registry, file_tools)
        register_patch_tools(registry, tool_context)
        register_shell_tools(registry, command_runner)
        _register_repo_and_catalog_tools(registry, root)

        selected_provider = provider
        if selected_provider is None and provider_factory is not None:
            selected_provider = provider_factory(provider_name, model, registry.schemas())
        if selected_provider is None:
            selected_provider = EchoProvider()

        session_path = Path(transcript_path) if transcript_path is not None else root / ".aetherion" / "session.jsonl"
        transcript = Transcript(session_path, cwd=str(root), entrypoint="aetherion-local")
        context = ContextManager(
            transcript,
            system_prompt=system_prompt,
            environment=_environment_block(root, provider_name, model),
            memory=_enabled_skill_context(),
            tools=registry.schemas(),
            context_window=context_window,
        )
        permissions = PermissionGate(mode=permission_mode, approval_callback=approval_callback)
        dispatcher = ToolDispatcher(registry, permissions=permissions, context=tool_context)
        bus = EventBus()
        engine = Engine(
            selected_provider,
            dispatcher,
            context,
            permissions,
            None,
            transcript,
            bus,
            AetherionConfig(max_steps=max_steps),
        )
        tool_context.cancel = engine.cancel
        controller = LocalEngineController(engine, permissions, bus)
        if approval_callback is None:
            permissions.approval_callback = controller.approval_callback
        return controller


def _environment_block(root: Path, provider: str, model: str) -> str:
    return "\n".join(
        (
            f"cwd: {root}",
            f"platform: {platform.system()} {platform.release()}",
            f"python: {platform.python_version()}",
            f"date: {date.today().isoformat()}",
            f"provider: {provider}",
            f"model: {model}",
        )
    )


def _enabled_skill_context() -> str:
    """Load only installed, explicitly enabled skill instructions.

    Skills are data, not imported Python modules.  Their content is bounded
    before it enters the model context, and malformed or unavailable library
    state simply contributes no optional memory.
    """

    try:
        from ..skill_library import SkillLibrary

        snippets: list[str] = []
        for entry in SkillLibrary().catalogue(installed_only=True):
            if not entry.enabled:
                continue
            loaded = SkillLibrary().get(entry.skill_id, include_content=True)
            content = (loaded.content if loaded is not None else "") or ""
            if content:
                snippets.append(f"[skill:{entry.skill_id}]\n{content[:12_000]}")
        return "\n\n".join(snippets)
    except Exception:
        return ""


async def _repo_map_handler(data: RepoMapInput, context: LocalToolContext) -> dict[str, Any]:
    index = index_repository(context.workspace)
    plan = build_reading_plan(index, data.query, data.token_budget, max_files=data.max_files)
    return plan.to_dict()


async def _integration_catalog_handler(data: IntegrationCatalogInput, context: LocalToolContext) -> dict[str, Any]:
    del context
    plugins: list[dict[str, Any]] = []
    skills: list[dict[str, Any]] = []
    try:
        from ..plugin_manager import PluginCatalogManager  # noqa: PLC0415

        plugins = [
            {key: item.get(key) for key in ("id", "name", "status", "execution_enabled", "integrity_status")}
            for item in PluginCatalogManager(evidence_only=True).list_installed()
        ]
    except Exception:
        plugins = []
    if data.include_skills:
        try:
            from ..skill_library import SkillLibrary  # noqa: PLC0415

            skills = [entry.to_dict(include_content=False) for entry in SkillLibrary().catalogue(installed_only=True)]
        except Exception:
            skills = []
    return {
        "plugins": plugins,
        "skills": skills,
        "rea": {
            "available": shutil.which("rea") is not None,
            "execution": "disabled-by-local-safety-policy",
        },
        "note": "Plugins remain quarantined metadata; enabled skills are loadable instructions, and REA requires explicit opt-in.",
    }


async def _skill_invoke_handler(data: SkillInvokeInput, _context: LocalToolContext) -> dict[str, Any]:
    """Expose an enabled skill as bounded instructions, never executable code."""

    from ..skill_library import SkillLibrary

    library = SkillLibrary()
    entry = library.get(data.skill_id, include_content=True)
    if entry is None or not entry.installed or not entry.enabled:
        raise ValueError(f"skill {data.skill_id!r} is not installed and enabled")
    return {
        "skill_id": entry.skill_id,
        "name": entry.name,
        "description": entry.description,
        "instructions": (entry.content or "")[:12_000],
        "content_sha256": entry.content_sha256,
    }


async def _plugin_inspect_handler(data: PluginInspectInput, _context: LocalToolContext) -> dict[str, Any]:
    """Return trusted plugin lifecycle metadata without importing plugin code."""

    from ..plugin_manager import PluginCatalogManager

    manager = PluginCatalogManager(evidence_only=True)
    for item in manager.list_installed():
        if str(item.get("id")) == data.plugin_id:
            return {
                key: item.get(key)
                for key in (
                    "id", "name", "source_url", "resolved_commit", "integrity_status",
                    "execution_enabled", "execution_status", "policy_status",
                )
            }
    raise ValueError(f"installed plugin {data.plugin_id!r} was not found")


async def _rea_inspect_handler(data: ReaInspectInput, context: LocalToolContext) -> dict[str, Any]:
    """Invoke the curated REA binary only after an explicit local opt-in.

    The default remains metadata-only.  When enabled, argv is passed directly
    to the binary (never through a shell) and inherits the workspace
    cancellation boundary.
    """

    executable = shutil.which("rea")
    if executable is None:
        return {"available": False, "execution": "unavailable"}
    if os.environ.get("AETHERION_ENABLE_REA") != "1":
        return {"available": True, "execution": "disabled", "reason": "set AETHERION_ENABLE_REA=1 to opt in"}
    result = await context.command_runner.run_argv(
        [executable, *data.args],
        cancel=context.cancel,
        timeout_seconds=120.0,
        cwd=context.workspace,
    )
    return {"available": True, "execution": "completed", "result": result.output, "error": result.error}


def _register_repo_and_catalog_tools(registry: ToolRegistry, workspace: Path) -> None:
    del workspace
    registry.register(
        "repo_map",
        "Build a bounded repository symbol/dependency reading plan",
        RepoMapInput,
        _repo_map_handler,
        is_read_only=True,
        is_concurrency_safe=True,
        risk_tier=1,
    )
    registry.register(
        "integration_catalog",
        "Inspect installed plugin and skill metadata without executing it",
        IntegrationCatalogInput,
        _integration_catalog_handler,
        is_read_only=True,
        is_concurrency_safe=True,
        risk_tier=1,
    )
    registry.register(
        "skill_invoke",
        "Load instructions from an installed and explicitly enabled skill",
        SkillInvokeInput,
        _skill_invoke_handler,
        is_read_only=True,
        is_concurrency_safe=True,
        risk_tier=1,
    )
    registry.register(
        "plugin_inspect",
        "Inspect installed plugin metadata without importing plugin code",
        PluginInspectInput,
        _plugin_inspect_handler,
        is_read_only=True,
        is_concurrency_safe=True,
        risk_tier=1,
    )
    registry.register(
        "rea_inspect",
        "Run the curated REA binary inspector after explicit local opt-in",
        ReaInspectInput,
        _rea_inspect_handler,
        is_read_only=True,
        is_concurrency_safe=False,
        risk_tier=2,
    )


__all__ = [
    "EchoProvider",
    "IntegrationCatalogInput",
    "PluginInspectInput",
    "ReaInspectInput",
    "SkillInvokeInput",
    "LocalEngineController",
    "LocalEngineFactory",
    "LocalToolContext",
    "RepoMapInput",
]

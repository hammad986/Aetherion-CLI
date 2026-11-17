"""Explicit argv and shell-string subprocess tools with cancellation safety."""

from __future__ import annotations

import asyncio
import os
import signal
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..engine.loop import ToolResult
from .registry import ToolRegistry


class ShellInput(BaseModel):
    argv: list[str] = Field(min_length=1)
    timeout_seconds: float = Field(default=120.0, gt=0, le=3600)


class BashInput(BaseModel):
    command: str = Field(min_length=1)
    timeout_seconds: float = Field(default=120.0, gt=0, le=3600)


class CommandRunner:
    def __init__(self, *, output_dir: Path | None = None, inline_limit: int = 100_000) -> None:
        self.output_dir = output_dir or Path(tempfile.gettempdir()) / "aetherion-tool-results"
        self.inline_limit = inline_limit

    async def run_argv(
        self,
        argv: Sequence[str],
        *,
        cancel: asyncio.Event | None = None,
        timeout_seconds: float = 120.0,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        on_output: Callable[[str, str], Awaitable[None] | None] | None = None,
    ) -> ToolResult:
        if not argv or any(not isinstance(part, str) or not part for part in argv):
            return ToolResult("", False, error="empty or invalid argv")
        return await self._run(
            list(argv),
            shell=False,
            cancel=cancel,
            timeout_seconds=timeout_seconds,
            cwd=cwd,
            env=env,
            on_output=on_output,
        )

    async def run_command(
        self,
        command: str,
        *,
        cancel: asyncio.Event | None = None,
        timeout_seconds: float = 120.0,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        on_output: Callable[[str, str], Awaitable[None] | None] | None = None,
    ) -> ToolResult:
        if not command.strip():
            return ToolResult("", False, error="empty command")
        return await self._run(
            command,
            shell=True,
            cancel=cancel,
            timeout_seconds=timeout_seconds,
            cwd=cwd,
            env=env,
            on_output=on_output,
        )

    async def _run(self, command: Any, *, shell: bool, cancel: asyncio.Event | None, timeout_seconds: float, cwd: str | os.PathLike[str] | None, env: Mapping[str, str] | None, on_output: Callable[[str, str], Awaitable[None] | None] | None) -> ToolResult:
        started = time.monotonic()
        process_env = _safe_environment(env)
        kwargs: dict[str, Any] = {"stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.PIPE, "cwd": cwd, "env": process_env}
        if os.name != "nt":
            kwargs["start_new_session"] = True
        try:
            if shell:
                process = await asyncio.create_subprocess_shell(command, **kwargs)
            else:
                process = await asyncio.create_subprocess_exec(*command, **kwargs)
        except OSError as exc:
            return ToolResult("", False, error=f"process start failed: {exc}")

        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []

        async def read_stream(stream: asyncio.StreamReader | None, label: str, target: list[bytes]) -> None:
            if stream is None:
                return
            while True:
                chunk = await stream.readline()
                if not chunk:
                    return
                target.append(chunk)
                if on_output is not None:
                    callback_result = on_output(label, chunk.decode("utf-8", errors="replace"))
                    if asyncio.iscoroutine(callback_result):
                        await callback_result

        readers = [
            asyncio.create_task(read_stream(process.stdout, "stdout", stdout_parts)),
            asyncio.create_task(read_stream(process.stderr, "stderr", stderr_parts)),
        ]
        wait_task = asyncio.create_task(process.wait())
        cancel_task = asyncio.create_task(cancel.wait()) if cancel is not None else None
        timed_out = False
        interrupted = False
        try:
            wait_set = {wait_task}
            if cancel_task is not None:
                wait_set.add(cancel_task)
            done, _ = await asyncio.wait(wait_set, timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED)
            if wait_task not in done:
                interrupted = cancel_task is not None and cancel_task in done
                timed_out = not interrupted
                await _terminate_tree(process)
                try:
                    await asyncio.wait_for(wait_task, timeout=2.0)
                except asyncio.TimeoutError:
                    await _kill_tree(process)
            await asyncio.gather(*readers, return_exceptions=True)
        finally:
            if cancel_task is not None:
                cancel_task.cancel()
            for task in readers:
                if not task.done():
                    task.cancel()
            if not wait_task.done():
                wait_task.cancel()

        stdout = b"".join(stdout_parts)
        stderr = b"".join(stderr_parts)
        persisted_path = None
        if len(stdout) + len(stderr) > self.inline_limit:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            persisted = self.output_dir / f"tool-{int(time.time() * 1000)}-{process.pid}.log"
            persisted.write_bytes(b"[stdout]\n" + stdout + b"\n[stderr]\n" + stderr)
            persisted_path = str(persisted)
            stdout = stdout[: self.inline_limit // 2]
            stderr = stderr[: self.inline_limit // 2]
        output = {
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "exit_code": process.returncode,
            "interrupted": interrupted,
            "timedOutAfterMs": int((time.monotonic() - started) * 1000) if timed_out else None,
            "persistedOutputPath": persisted_path,
            "persistedOutputSize": len(stdout) + len(stderr),
        }
        ok = process.returncode == 0 and not interrupted and not timed_out
        error = None if ok else ("cancelled" if interrupted else "timed out" if timed_out else f"exit code {process.returncode}")
        return ToolResult("", ok, output=output, error=error)


async def _terminate_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


async def _kill_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _safe_environment(env: Mapping[str, str] | None) -> dict[str, str]:
    result = dict(os.environ)
    if env:
        result.update({str(key): str(value) for key, value in env.items()})
    blocked_prefixes = ("LD_", "DYLD_", "GIT_CONFIG_", "PYTHONPATH", "NODE_PATH", "RUSTFLAGS", "CFLAGS", "CXXFLAGS")
    return {key: value for key, value in result.items() if not key.startswith(blocked_prefixes)}


async def _shell_handler(data: ShellInput, ctx: Any, runner: CommandRunner | None = None) -> ToolResult:
    runner = runner or (ctx if isinstance(ctx, CommandRunner) else getattr(ctx, "command_runner", None))
    if not isinstance(runner, CommandRunner):
        runner = CommandRunner()
    return await runner.run_argv(
        data.argv,
        cancel=getattr(ctx, "cancel", None),
        timeout_seconds=data.timeout_seconds,
        on_output=getattr(ctx, "on_tool_output", None),
    )


async def _bash_handler(data: BashInput, ctx: Any, runner: CommandRunner | None = None) -> ToolResult:
    runner = runner or (ctx if isinstance(ctx, CommandRunner) else getattr(ctx, "command_runner", None))
    if not isinstance(runner, CommandRunner):
        runner = CommandRunner()
    return await runner.run_command(
        data.command,
        cancel=getattr(ctx, "cancel", None),
        timeout_seconds=data.timeout_seconds,
        on_output=getattr(ctx, "on_tool_output", None),
    )


def register_shell_tools(registry: ToolRegistry, runner: CommandRunner | None = None) -> None:
    runner = runner or CommandRunner()
    async def shell_handler(data: ShellInput, ctx: Any) -> ToolResult:
        return await _shell_handler(data, ctx, runner)

    async def bash_handler(data: BashInput, ctx: Any) -> ToolResult:
        return await _bash_handler(data, ctx, runner)

    registry.register("shell", "Execute an argv command without a shell", ShellInput, shell_handler, is_read_only=False, is_concurrency_safe=False, risk_tier=3)
    registry.register("bash", "Execute a shell command string", BashInput, bash_handler, is_read_only=False, is_concurrency_safe=False, risk_tier=3)

"""Level-by-level asynchronous execution for validated planning DAGs."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from enum import Enum
from typing import Any, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from .dag import PlanTask, TaskDAG


class TaskStatus(str, Enum):
    """Lifecycle state of one planned task."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStatus(str, Enum):
    """Terminal state of one scheduler run."""

    COMPLETED = "completed"
    FAILED = "failed"


class PlanEventType(str, Enum):
    """Transcript-safe lifecycle events emitted by :class:`PlanScheduler`."""

    PLAN_STARTED = "plan_started"
    LEVEL_STARTED = "level_started"
    TASK_STARTED = "task_started"
    TASK_FINISHED = "task_finished"
    TASK_FAILED = "task_failed"
    TASK_SKIPPED = "task_skipped"
    LEVEL_FINISHED = "level_finished"
    PLAN_FINISHED = "plan_finished"
    PLAN_FAILED = "plan_failed"


class TaskOutcome(BaseModel):
    """Optional structured return value for an injected task runner."""

    model_config = ConfigDict(extra="forbid", strict=True)

    success: bool = True
    output: Any = None
    error: str | None = None


class PlanEvent(BaseModel):
    """Immutable, JSON-serializable scheduler event."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    type: PlanEventType
    level: int | None = None
    task_id: str | None = None
    status: TaskStatus | PlanStatus | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def event_type(self) -> PlanEventType:
        """Explicitly named alias for consumers that avoid the ``type`` field."""

        return self.type

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible event for transcript appenders."""

        return self.model_dump(mode="json", exclude_none=True)

    @classmethod
    def json_schema_for_tools(cls) -> dict[str, Any]:
        """Return the event schema suitable for a tool or model contract."""

        return cls.model_json_schema(mode="validation")


class TaskExecution(BaseModel):
    """Final observable outcome for one task in an execution report."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: str
    level: int
    status: TaskStatus
    output: Any = None
    error: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible task outcome."""

        return self.model_dump(mode="json", exclude_none=True)


class ExecutionReport(BaseModel):
    """Complete scheduler result, including events needed for transcript replay."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: PlanStatus
    task_results: tuple[TaskExecution, ...] = Field(default_factory=tuple)
    events: tuple[PlanEvent, ...] = Field(default_factory=tuple)

    @property
    def succeeded(self) -> bool:
        """Whether every task completed successfully."""

        return self.status is PlanStatus.COMPLETED

    @property
    def results(self) -> tuple[TaskExecution, ...]:
        """Compatibility alias for callers that call task outcomes results."""

        return self.task_results

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible execution report."""

        return self.model_dump(mode="json")


TaskRunner: TypeAlias = Callable[[PlanTask], object | Awaitable[object]]
EventCallback: TypeAlias = Callable[[PlanEvent], object | Awaitable[object]]
AppendCallback: TypeAlias = Callable[..., object | Awaitable[object]]


class PlanScheduler:
    """Execute a validated task DAG in deterministic, conflict-free batches.

    Tasks in a topological level are split into lexical batches so declared
    files never overlap within one ``asyncio.gather`` call.  A task exception,
    an explicit unsuccessful :class:`TaskOutcome`, or a literal ``False``
    stops scheduling.  Already-running peers finish, while every pending task
    is recorded as skipped and all transitive dependents of the failed task are
    explicitly identified in their skip reason.

    ``emit`` receives typed :class:`PlanEvent` objects.  A one-argument
    ``append`` callback receives their JSON-compatible dictionaries, making
    ``list.append`` straightforward.  A two-argument append callback receives
    ``(event_type, payload)`` and can be an existing transcript's ``append``
    method.  All callbacks may be synchronous or asynchronous.
    """

    def __init__(
        self,
        dag: TaskDAG,
        runner: TaskRunner,
        *,
        emit: EventCallback | None = None,
        append: AppendCallback | None = None,
        on_event: EventCallback | None = None,
        event_callback: EventCallback | None = None,
        append_callback: AppendCallback | None = None,
        append_event: AppendCallback | None = None,
    ) -> None:
        if not isinstance(dag, TaskDAG):
            raise TypeError("dag must be a TaskDAG")
        if not callable(runner):
            raise TypeError("runner must be callable")
        self.dag = dag
        self.runner = runner
        self.emit = _coalesce_callback(emit, on_event, event_callback, label="event")
        self.append = _coalesce_callback(append, append_callback, append_event, label="append")
        self._events: list[PlanEvent] = []

    @property
    def events(self) -> tuple[PlanEvent, ...]:
        """Events emitted during the most recent run."""

        return tuple(self._events)

    async def run(self) -> ExecutionReport:
        """Execute the DAG and return a complete, transcript-ready report."""

        self._events = []
        statuses = {task_id: TaskStatus.PENDING for task_id in self.dag.task_ids}
        outcomes: dict[str, TaskExecution] = {}
        await self._publish(
            PlanEventType.PLAN_STARTED,
            data={"task_count": len(self.dag.tasks)},
        )

        failed_task_id: str | None = None
        failure_error: str | None = None
        for level_number, level_ids in enumerate(self.dag.levels):
            if failed_task_id is not None:
                break
            await self._publish(
                PlanEventType.LEVEL_STARTED,
                level=level_number,
                data={"task_ids": list(level_ids)},
            )
            remaining = set(level_ids)
            while remaining and failed_task_id is None:
                candidates = [self.dag.get(task_id) for task_id in sorted(remaining)]
                batch = self.dag.select_parallel(candidates)
                if not batch:
                    raise RuntimeError("scheduler could not select a runnable task batch")
                remaining.difference_update(task.id for task in batch)
                for task in batch:
                    statuses[task.id] = TaskStatus.RUNNING
                    await self._publish(
                        PlanEventType.TASK_STARTED,
                        level=level_number,
                        task_id=task.id,
                        status=TaskStatus.RUNNING,
                        data={
                            "risk_tier": task.risk_tier.value,
                            "declared_files": list(task.declared_files),
                        },
                    )
                batch_outcomes = await self._run_batch(batch)
                for task, outcome, error in batch_outcomes:
                    if error is not None:
                        statuses[task.id] = TaskStatus.FAILED
                        if failed_task_id is None:
                            failed_task_id = task.id
                            failure_error = error
                        result = TaskExecution(
                            task_id=task.id,
                            level=level_number,
                            status=TaskStatus.FAILED,
                            error=error,
                        )
                        outcomes[task.id] = result
                        await self._publish(
                            PlanEventType.TASK_FAILED,
                            level=level_number,
                            task_id=task.id,
                            status=TaskStatus.FAILED,
                            data={"error": error},
                        )
                    else:
                        statuses[task.id] = TaskStatus.SUCCEEDED
                        result = TaskExecution(
                            task_id=task.id,
                            level=level_number,
                            status=TaskStatus.SUCCEEDED,
                            output=outcome,
                        )
                        outcomes[task.id] = result
                        await self._publish(
                            PlanEventType.TASK_FINISHED,
                            level=level_number,
                            task_id=task.id,
                            status=TaskStatus.SUCCEEDED,
                            data={"output": outcome},
                        )
            if failed_task_id is None:
                await self._publish(
                    PlanEventType.LEVEL_FINISHED,
                    level=level_number,
                    data={"task_ids": list(level_ids)},
                )

        if failed_task_id is not None:
            downstream = set(self.dag.downstream_of(failed_task_id))
            for task_id in self.dag.task_ids:
                if statuses[task_id] is not TaskStatus.PENDING:
                    continue
                task = self.dag.get(task_id)
                reason = (
                    f"skipped because dependency {failed_task_id!r} failed"
                    if task_id in downstream
                    else f"plan halted after task {failed_task_id!r} failed"
                )
                statuses[task_id] = TaskStatus.SKIPPED
                outcomes[task_id] = TaskExecution(
                    task_id=task_id,
                    level=_level_for(self.dag, task_id),
                    status=TaskStatus.SKIPPED,
                    reason=reason,
                )
                await self._publish(
                    PlanEventType.TASK_SKIPPED,
                    level=_level_for(self.dag, task_id),
                    task_id=task_id,
                    status=TaskStatus.SKIPPED,
                    data={"reason": reason},
                )
            await self._publish(
                PlanEventType.PLAN_FAILED,
                status=PlanStatus.FAILED,
                data={"task_id": failed_task_id, "error": failure_error or "task failed"},
            )
            report_status = PlanStatus.FAILED
        else:
            await self._publish(
                PlanEventType.PLAN_FINISHED,
                status=PlanStatus.COMPLETED,
                data={"task_count": len(self.dag.tasks)},
            )
            report_status = PlanStatus.COMPLETED

        ordered_results = tuple(outcomes[task_id] for task_id in self.dag.task_ids)
        return ExecutionReport(
            status=report_status, task_results=ordered_results, events=self.events
        )

    async def execute(self) -> ExecutionReport:
        """Alias for :meth:`run` for execution-oriented callers."""

        return await self.run()

    async def _run_batch(
        self, batch: tuple[PlanTask, ...]
    ) -> tuple[tuple[PlanTask, object | None, str | None], ...]:
        raw_outcomes = await asyncio_gather(
            *(self._run_task(task) for task in batch),
        )
        return tuple((task, output, error) for task, (output, error) in zip(batch, raw_outcomes))

    async def _run_task(self, task: PlanTask) -> tuple[object | None, str | None]:
        try:
            raw = self.runner(task)
            value = await raw if inspect.isawaitable(raw) else raw
            if isinstance(value, TaskOutcome):
                if not value.success:
                    return value.output, value.error or "task returned an unsuccessful outcome"
                return value.output, None
            if value is False:
                return value, "task runner returned False"
            return value, None
        except Exception as exc:  # noqa: BLE001 - task failures are reportable outcomes
            return None, f"{type(exc).__name__}: {exc}"

    async def _publish(
        self,
        event_type: PlanEventType,
        *,
        level: int | None = None,
        task_id: str | None = None,
        status: TaskStatus | PlanStatus | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        event = PlanEvent(
            type=event_type,
            level=level,
            task_id=task_id,
            status=status,
            data=dict(data or {}),
        )
        self._events.append(event)
        if self.emit is not None:
            await _maybe_await(self.emit(event))
        if self.append is not None:
            await self._append_event(event)

    async def _append_event(self, event: PlanEvent) -> None:
        if self.append is None:
            return
        payload = event.to_dict()
        if _accepts_two_arguments(self.append):
            await _maybe_await(self.append(event.type.value, payload))
        else:
            await _maybe_await(self.append(payload))


async def asyncio_gather(
    *awaitables: Awaitable[tuple[object | None, str | None]],
) -> tuple[tuple[object | None, str | None], ...]:
    """Gather task outcomes while retaining deterministic input ordering."""

    return tuple(await asyncio.gather(*awaitables))


async def _maybe_await(value: object | Awaitable[object]) -> object:
    return await value if inspect.isawaitable(value) else value


def _coalesce_callback(
    *callbacks: Callable[..., object] | None,
    label: str,
) -> Callable[..., object] | None:
    selected = [callback for callback in callbacks if callback is not None]
    if len(selected) > 1:
        raise TypeError(f"provide at most one {label} callback")
    return selected[0] if selected else None


def _accepts_two_arguments(callback: Callable[..., object]) -> bool:
    try:
        parameters = tuple(inspect.signature(callback).parameters.values())
    except (TypeError, ValueError):
        return False
    positional = tuple(
        parameter
        for parameter in parameters
        if parameter.kind
        in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    )
    return len(positional) >= 2 or any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters
    )


def _level_for(dag: TaskDAG, task_id: str) -> int:
    for level, task_ids in enumerate(dag.levels):
        if task_id in task_ids:
            return level
    raise KeyError(task_id)


Scheduler = PlanScheduler

__all__ = [
    "AppendCallback",
    "EventCallback",
    "ExecutionReport",
    "PlanEvent",
    "PlanEventType",
    "PlanScheduler",
    "PlanStatus",
    "Scheduler",
    "TaskExecution",
    "TaskOutcome",
    "TaskRunner",
    "TaskStatus",
]

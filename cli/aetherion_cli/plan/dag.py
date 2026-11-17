"""Strict task and dependency-graph models for Phase 7 planning.

The graph is deliberately independent from task execution.  A model can build
and serialize a plan without importing the scheduler, while the scheduler can
rely on the graph having already rejected malformed dependencies and cycles.
"""

from __future__ import annotations

import os
import posixpath
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


class RiskTier(str, Enum):
    """Risk classification attached to a planned task."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PlanTask(BaseModel):
    """A serializable, dependency-aware unit of planned work.

    ``task_id``/``step_id``, ``objective``, ``depends_on``, ``files``/
    ``target_files``, and ``risk``/``risk_level`` are accepted as input aliases
    for model/tool callers.  Canonical serialized output always uses ``id``,
    ``description``, ``dependencies``, ``declared_files``, and ``risk_tier``.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )

    id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("id", "task_id", "step_id"),
        serialization_alias="id",
    )
    description: str = Field(
        default="",
        validation_alias=AliasChoices("description", "objective"),
        serialization_alias="description",
    )
    dependencies: tuple[str, ...] = Field(
        default_factory=tuple,
        validation_alias=AliasChoices("dependencies", "depends_on"),
        serialization_alias="dependencies",
    )
    declared_files: tuple[str, ...] = Field(
        default_factory=tuple,
        validation_alias=AliasChoices("declared_files", "files", "target_files"),
        serialization_alias="declared_files",
    )
    risk_tier: RiskTier = Field(
        default=RiskTier.LOW,
        validation_alias=AliasChoices("risk_tier", "risk", "risk_level"),
        serialization_alias="risk_tier",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "description")
    @classmethod
    def _reject_blank_text(cls, value: str, info: Any) -> str:
        if info.field_name == "id" or value:
            if not value.strip():
                raise ValueError(f"{info.field_name} must not be blank")
        return value

    @field_validator("dependencies", "declared_files", mode="before")
    @classmethod
    def _accept_json_arrays(cls, values: Any) -> tuple[str, ...]:
        if isinstance(values, list):
            return tuple(values)
        if isinstance(values, tuple):
            return values
        raise ValueError("must be an array of strings")

    @field_validator("dependencies", "declared_files")
    @classmethod
    def _reject_blank_or_duplicate_values(
        cls, values: tuple[str, ...], info: Any
    ) -> tuple[str, ...]:
        field_name = info.field_name
        for value in values:
            if not value.strip():
                raise ValueError(f"{field_name} must not contain blank values")
        if len(values) != len(set(values)):
            raise ValueError(f"{field_name} must not contain duplicates")
        return values

    @field_validator("risk_tier", mode="before")
    @classmethod
    def _parse_risk_tier(cls, value: RiskTier | str) -> RiskTier:
        if isinstance(value, RiskTier):
            return value
        if isinstance(value, str):
            try:
                return RiskTier(value)
            except ValueError as exc:
                raise ValueError(f"unknown risk tier: {value!r}") from exc
        raise ValueError("risk_tier must be a risk tier string")

    @property
    def task_id(self) -> str:
        """Compatibility accessor for callers that use the explicit name."""

        return self.id

    @property
    def step_id(self) -> str:
        """Compatibility accessor for graph callers that use step terminology."""

        return self.id

    @property
    def objective(self) -> str:
        """Compatibility accessor for objective-oriented plan callers."""

        return self.description

    @property
    def depends_on(self) -> tuple[str, ...]:
        """Compatibility accessor for dependency-oriented callers."""

        return self.dependencies

    @property
    def files(self) -> tuple[str, ...]:
        """Compatibility accessor for the declared file set."""

        return self.declared_files

    @property
    def target_files(self) -> tuple[str, ...]:
        """Compatibility accessor for mutation-scope callers."""

        return self.declared_files

    @property
    def risk_level(self) -> RiskTier:
        """Compatibility accessor for risk-level terminology."""

        return self.risk_tier

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical JSON-compatible task representation."""

        return self.model_dump(mode="json", by_alias=True)

    @classmethod
    def json_schema_for_tools(cls) -> dict[str, Any]:
        """Return the model schema suitable for a tool or model contract."""

        return cls.model_json_schema(mode="validation")


class TaskDAG(BaseModel):
    """Validated directed acyclic graph of :class:`PlanTask` objects."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )

    tasks: tuple[PlanTask, ...] = Field(default_factory=tuple)

    @field_validator("tasks", mode="before")
    @classmethod
    def _accept_json_task_array(cls, values: Any) -> tuple[PlanTask, ...]:
        if isinstance(values, list):
            return tuple(values)
        if isinstance(values, tuple):
            return values
        raise ValueError("tasks must be an array")

    @model_validator(mode="after")
    def _validate_graph(self) -> TaskDAG:
        task_ids = [task.id for task in self.tasks]
        duplicates = _duplicates(task_ids)
        if duplicates:
            names = ", ".join(sorted(duplicates))
            raise ValueError(f"duplicate task id(s): {names}")

        known_ids = set(task_ids)
        for task in self.tasks:
            unknown = sorted(set(task.dependencies) - known_ids)
            if unknown:
                names = ", ".join(unknown)
                raise ValueError(f"task {task.id!r} has unknown dependency/dependencies: {names}")
            if task.id in task.dependencies:
                raise ValueError(f"task {task.id!r} cannot depend on itself")

        _topological_id_levels(self.tasks)
        return self

    @property
    def task_ids(self) -> tuple[str, ...]:
        """Task identifiers in deterministic lexical order."""

        return tuple(sorted(task.id for task in self.tasks))

    @property
    def task_map(self) -> Mapping[str, PlanTask]:
        """Read-only-by-convention task lookup keyed by task identifier."""

        return {task.id: task for task in self.tasks}

    @property
    def levels(self) -> tuple[tuple[str, ...], ...]:
        """Topological levels as deterministic tuples of task identifiers."""

        return self.topological_levels()

    def topological_levels(self) -> tuple[tuple[str, ...], ...]:
        """Return dependency-safe levels, with lexical ordering within levels."""

        return _topological_id_levels(self.tasks)

    def task_levels(self) -> tuple[tuple[PlanTask, ...], ...]:
        """Return the same levels with their complete task models."""

        tasks = self.task_map
        return tuple(tuple(tasks[task_id] for task_id in level) for level in self.levels)

    def get(self, task_id: str) -> PlanTask:
        """Return a task by id or raise a clear unknown-task error."""

        try:
            return self.task_map[task_id]
        except KeyError as exc:
            raise KeyError(f"unknown task: {task_id}") from exc

    def dependents_of(self, task_id: str) -> tuple[str, ...]:
        """Return direct dependents of ``task_id`` in lexical order."""

        self.get(task_id)
        return tuple(sorted(task.id for task in self.tasks if task_id in task.dependencies))

    def downstream_of(self, task_id: str) -> tuple[str, ...]:
        """Return every transitive dependent of ``task_id`` deterministically."""

        self.get(task_id)
        found: set[str] = set()
        frontier = [task_id]
        while frontier:
            current = frontier.pop(0)
            for dependent in self.dependents_of(current):
                if dependent not in found:
                    found.add(dependent)
                    frontier.append(dependent)
        return tuple(sorted(found))

    def select_parallel(self, candidates: Iterable[str | PlanTask]) -> tuple[PlanTask, ...]:
        """Select the deterministic, non-overlapping subset of candidates.

        Candidates are considered in lexical task-id order.  A task is selected
        only when none of its declared files overlap a file already selected in
        this batch.  Empty file declarations never conflict.
        """

        candidate_tasks = _resolve_candidates(self, candidates)
        return _select_parallel_tasks(candidate_tasks)

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical JSON-compatible graph representation."""

        return self.model_dump(mode="json", by_alias=True)

    @classmethod
    def json_schema_for_tools(cls) -> dict[str, Any]:
        """Return the graph schema suitable for a tool or model contract."""

        return cls.model_json_schema(mode="validation")


def select_parallel_tasks(candidates: Iterable[PlanTask]) -> tuple[PlanTask, ...]:
    """Select a deterministic non-overlapping batch from task models."""

    return _select_parallel_tasks(tuple(candidates))


def _resolve_candidates(dag: TaskDAG, candidates: Iterable[str | PlanTask]) -> tuple[PlanTask, ...]:
    resolved: list[PlanTask] = []
    seen: set[str] = set()
    for candidate in candidates:
        task = dag.get(candidate) if isinstance(candidate, str) else candidate
        if task.id not in dag.task_map:
            raise ValueError(f"candidate task is not part of this graph: {task.id}")
        if task.id not in seen:
            resolved.append(task)
            seen.add(task.id)
    return tuple(resolved)


def _select_parallel_tasks(candidates: Iterable[PlanTask]) -> tuple[PlanTask, ...]:
    selected: list[PlanTask] = []
    claimed_files: set[str] = set()
    seen_ids: set[str] = set()
    for task in sorted(candidates, key=lambda item: item.id):
        if task.id in seen_ids:
            continue
        seen_ids.add(task.id)
        files = {_file_key(path) for path in task.declared_files}
        if files.isdisjoint(claimed_files):
            selected.append(task)
            claimed_files.update(files)
    return tuple(selected)


def _topological_id_levels(tasks: Iterable[PlanTask]) -> tuple[tuple[str, ...], ...]:
    task_list = tuple(tasks)
    remaining = {task.id: set(task.dependencies) for task in task_list}
    levels: list[tuple[str, ...]] = []
    while remaining:
        ready = tuple(
            sorted(task_id for task_id, dependencies in remaining.items() if not dependencies)
        )
        if not ready:
            cycle = ", ".join(sorted(remaining))
            raise ValueError(f"task graph contains a cycle involving: {cycle}")
        levels.append(ready)
        for task_id in ready:
            del remaining[task_id]
        for dependencies in remaining.values():
            dependencies.difference_update(ready)
    return tuple(levels)


def _duplicates(values: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _file_key(path: str) -> str:
    """Normalize a declared path for conflict comparison without filesystem IO."""

    normalized = posixpath.normpath(path.replace("\\", "/"))
    return os.path.normcase(normalized)


Task = PlanTask
TaskSpec = PlanTask
DAG = TaskDAG

__all__ = [
    "DAG",
    "PlanTask",
    "RiskTier",
    "Task",
    "TaskDAG",
    "TaskSpec",
    "select_parallel_tasks",
]

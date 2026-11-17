"""Provider-aware token accounting and deterministic budget slicing."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(slots=True, frozen=True)
class UsageSnapshot:
    """Normalized provider usage, including cached input that still occupies the window."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @classmethod
    def from_value(cls, value: Any) -> UsageSnapshot:
        if value is None:
            return cls()
        if isinstance(value, Mapping):
            get = value.get
        else:
            def get(key: str, default: int = 0) -> Any:
                return getattr(value, key, default)
        return cls(
            input_tokens=max(0, int(get("input_tokens", 0) or 0)),
            output_tokens=max(0, int(get("output_tokens", 0) or 0)),
            cache_read_input_tokens=max(0, int(get("cache_read_input_tokens", 0) or 0)),
            cache_creation_input_tokens=max(0, int(get("cache_creation_input_tokens", 0) or 0)),
        )

    @property
    def window_tokens(self) -> int:
        return self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens


@dataclass(slots=True, frozen=True)
class BudgetSlices:
    """Exact token allocations for the request assembly pipeline."""

    system_environment: int
    repo_map: int
    memory: int
    summary: int
    conversation: int
    output_reserve: int


@dataclass(slots=True)
class TokenBudget:
    """Token window policy with provider usage and preflight estimation support."""

    context_window: int = 200_000
    reserved_output: int = 16_384
    safety_margin: int = 1_024
    micro_threshold: float = 0.70
    full_threshold: float = 0.92
    system_environment_ratio: float = 0.08
    repo_map_ratio: float = 0.04
    memory_ratio: float = 0.05
    summary_ratio: float = 0.08
    last_usage: UsageSnapshot = field(default_factory=UsageSnapshot)
    last_estimate: int = 0
    compaction_count: int = 0
    mode: str = "normal"

    def __post_init__(self) -> None:
        if self.context_window <= 0:
            raise ValueError("context_window must be positive")
        if self.reserved_output < 0 or self.safety_margin < 0:
            raise ValueError("reserved_output and safety_margin must be non-negative")
        if self.effective_input_budget <= 0:
            raise ValueError("reserved output and safety margin exceed context window")
        if not 0 < self.micro_threshold < self.full_threshold <= 1:
            raise ValueError("thresholds must satisfy 0 < micro < full <= 1")
        ratios = (self.system_environment_ratio, self.repo_map_ratio, self.memory_ratio, self.summary_ratio)
        if any(ratio < 0 for ratio in ratios) or sum(ratios) >= 1:
            raise ValueError("context slice ratios must be non-negative and leave conversation capacity")

    @property
    def effective_input_budget(self) -> int:
        return self.context_window - self.reserved_output - self.safety_margin

    @property
    def utilization(self) -> float:
        return self.last_usage.window_tokens / self.effective_input_budget

    def usage_ratio(self, usage: Any) -> float:
        snapshot = UsageSnapshot.from_value(usage)
        self.last_usage = snapshot
        return snapshot.window_tokens / self.effective_input_budget

    def record_usage(self, usage: Any) -> UsageSnapshot:
        self.usage_ratio(usage)
        return self.last_usage

    def record_estimate(self, estimate: int) -> int:
        if estimate < 0:
            raise ValueError("token estimate cannot be negative")
        self.last_estimate = estimate
        return estimate

    def estimate_ratio(self, estimate: int) -> float:
        return self.record_estimate(estimate) / self.effective_input_budget

    def needs_micro_compaction(self, estimate: int | None = None) -> bool:
        ratio = self.estimate_ratio(estimate) if estimate is not None else self.utilization
        return self.micro_threshold <= ratio < self.full_threshold

    def needs_full_compaction(self, estimate: int | None = None) -> bool:
        ratio = self.estimate_ratio(estimate) if estimate is not None else self.utilization
        return ratio >= self.full_threshold

    def record_compaction(self, mode: str) -> None:
        if mode not in {"micro", "full", "reactive"}:
            raise ValueError("compaction mode must be micro, full, or reactive")
        self.compaction_count += 1
        self.mode = mode

    def slices(self) -> BudgetSlices:
        available = self.effective_input_budget
        system = int(available * self.system_environment_ratio)
        repo = int(available * self.repo_map_ratio)
        memory = int(available * self.memory_ratio)
        summary = int(available * self.summary_ratio)
        conversation = max(0, available - system - repo - memory - summary)
        return BudgetSlices(system, repo, memory, summary, conversation, self.reserved_output)


def estimate_tokens(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
    system: str,
    tokenizer: Callable[[str], int] | None = None,
) -> int:
    """Estimate request tokens, including serialized tool schemas.

    A model-specific tokenizer can be supplied. The fallback is deliberately
    conservative and deterministic rather than pretending to know a provider's
    exact tokenizer.
    """

    def count(value: str) -> int:
        if tokenizer is not None:
            return max(0, int(tokenizer(value)))
        return max(1, (len(value) + 3) // 4) if value else 0

    total = count(system)
    total += sum(count(json.dumps(tool, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) for tool in tools)
    total += sum(count(json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) for message in messages)
    return total

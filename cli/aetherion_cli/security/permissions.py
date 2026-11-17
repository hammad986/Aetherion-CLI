"""Hierarchical deny/ask/allow permission evaluation with session grants."""

from __future__ import annotations

import fnmatch
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..engine.loop import ToolCall
from .risk import RiskAssessment, RiskTier, classify_action


class PermissionEffect(str, Enum):
    DENY = "deny"
    ASK = "ask"
    ALLOW = "allow"


class PermissionMode(str, Enum):
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    PLAN = "plan"
    AUTO = "auto"
    DONT_ASK = "dontAsk"
    BYPASS = "bypassPermissions"


@dataclass(slots=True, frozen=True)
class PermissionRule:
    specifier: str
    effect: PermissionEffect
    source: str = "session"
    priority: int = 0

    def matches(self, tool_name: str) -> bool:
        return fnmatch.fnmatchcase(tool_name, self.specifier)

    @property
    def specificity(self) -> int:
        return sum(1 for char in self.specifier if char not in "*?")


@dataclass(slots=True, frozen=True)
class PermissionDecision:
    effect: PermissionEffect
    reason: str
    risk: RiskAssessment | None = None
    suggested_rule: str | None = None
    rewritten_input: Any = None

    @property
    def allowed(self) -> bool:
        return self.effect is PermissionEffect.ALLOW

    @classmethod
    def allow(cls, reason: str, *, risk: RiskAssessment | None = None) -> PermissionDecision:
        return cls(PermissionEffect.ALLOW, reason, risk)

    @classmethod
    def ask(cls, reason: str, *, risk: RiskAssessment | None = None, suggested_rule: str | None = None) -> PermissionDecision:
        return cls(PermissionEffect.ASK, reason, risk, suggested_rule)

    @classmethod
    def deny(cls, reason: str, *, risk: RiskAssessment | None = None) -> PermissionDecision:
        return cls(PermissionEffect.DENY, reason, risk)


ApprovalCallback = Callable[[PermissionDecision, ToolCall, Any], Any]


class PermissionGate:
    def __init__(
        self,
        *,
        mode: PermissionMode | str = PermissionMode.DEFAULT,
        rules: list[PermissionRule] | None = None,
        approval_callback: ApprovalCallback | None = None,
        classifier: Callable[[str, Any], RiskAssessment] = classify_action,
    ) -> None:
        self.mode = PermissionMode(mode)
        self.rules = list(rules or [])
        self.approval_callback = approval_callback
        self.classifier = classifier
        self.session_grants: dict[str, PermissionEffect] = {}
        self.hooks: list[Callable[[ToolCall, Any], Any]] = []

    def add_rule(self, rule: PermissionRule) -> None:
        self.rules.append(rule)

    def grant(self, specifier: str) -> None:
        self.session_grants[specifier] = PermissionEffect.ALLOW

    async def check(self, call: ToolCall, engine: Any, spec: Any = None) -> PermissionDecision:
        risk = self.classifier(call.name, dict(call.arguments))
        if call.metadata.get("hard_deny"):
            return PermissionDecision.deny("hard safety policy denial", risk=risk)
        if self.mode is PermissionMode.PLAN and risk.tier is not RiskTier.TIER_1:
            return PermissionDecision.deny("plan mode permits read-only tools only", risk=risk)

        for hook in self.hooks:
            result = hook(call, engine)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, PermissionDecision):
                if result.effect is PermissionEffect.DENY:
                    return result
            elif result is False:
                return PermissionDecision.deny("pre-tool hook denied the action", risk=risk)

        matching = sorted(
            (rule for rule in self.rules if rule.matches(call.name)),
            key=lambda rule: (rule.priority, rule.specificity),
            reverse=True,
        )
        deny = next((rule for rule in matching if rule.effect is PermissionEffect.DENY), None)
        if deny:
            return PermissionDecision.deny(f"denied by rule {deny.specifier}", risk=risk)
        if matching:
            selected = matching[0]
            if selected.effect is PermissionEffect.ALLOW:
                return PermissionDecision.allow(f"allowed by rule {selected.specifier}", risk=risk)
            return PermissionDecision.ask(f"approval required by rule {selected.specifier}", risk=risk, suggested_rule=selected.specifier)
        if any(fnmatch.fnmatchcase(call.name, pattern) for pattern in self.session_grants):
            return PermissionDecision.allow("allowed by session grant", risk=risk)
        if self.mode is PermissionMode.BYPASS:
            return PermissionDecision.allow("explicit bypass permission mode", risk=risk)
        if risk.tier is RiskTier.TIER_1:
            return PermissionDecision.allow("read-only action", risk=risk)
        if self.mode is PermissionMode.ACCEPT_EDITS and risk.tier is RiskTier.TIER_2:
            return PermissionDecision.allow("reversible edit accepted by session mode", risk=risk)
        if self.mode in {PermissionMode.DONT_ASK, PermissionMode.AUTO}:
            return PermissionDecision.deny("unresolved action denied by non-interactive mode", risk=risk)
        return PermissionDecision.ask("explicit approval required", risk=risk, suggested_rule=call.name)

    async def resolve(self, decision: PermissionDecision, call: ToolCall, engine: Any) -> PermissionDecision:
        if decision.effect is not PermissionEffect.ASK:
            return decision
        if self.approval_callback is None:
            return PermissionDecision.deny("approval callback is not configured", risk=decision.risk)
        result = self.approval_callback(decision, call, engine)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, PermissionDecision):
            return result
        if result is True or result == "allow":
            return PermissionDecision.allow("approved by user", risk=decision.risk)
        if result in {"always", "allow_session"}:
            self.grant(call.name)
            return PermissionDecision.allow("approved and granted for this session", risk=decision.risk)
        return PermissionDecision.deny("denied by user", risk=decision.risk)

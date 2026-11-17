"""Explainable three-tier action and shell risk classification."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class RiskTier(IntEnum):
    TIER_1 = 1
    TIER_2 = 2
    TIER_3 = 3


@dataclass(slots=True, frozen=True)
class RiskAssessment:
    tier: RiskTier
    reason: str
    command: str = ""
    matched_rules: tuple[str, ...] = ()


_CRITICAL = re.compile(r"(?:rm\s+-rf|git\s+(?:reset\s+--hard|clean\s+-f|push\s+.*--force)|shutdown|reboot|mkfs|dd\s+if=|format\s+)", re.I)
_STRUCTURE = re.compile(r"(?:;|&&|\|\||\||&|\n|>>?|<<|`|\$\(|\b(?:sudo|env|exec|nohup|timeout|bash\s+-c|sh\s+-c|python\s+-c|node\s+-e|eval|xargs|find\s+-exec)\b)")


def classify_action(tool_name: str, arguments: Any) -> RiskAssessment:
    normalized = tool_name.lower()
    if normalized in {"read_file", "grep", "search", "glob", "list_dir", "view_image"}:
        return RiskAssessment(RiskTier.TIER_1, "read-only inspection")
    if normalized in {"write_file", "edit_file", "apply_patch", "update_plan"}:
        return RiskAssessment(RiskTier.TIER_2, "reversible or reviewable state change")
    if normalized in {"bash", "shell", "delete", "remove_file"}:
        command = str(arguments.get("command", "") if isinstance(arguments, dict) else arguments)
        return classify_command(command)
    return RiskAssessment(RiskTier.TIER_3, "unknown tool defaults to explicit approval")


def classify_command(command: str) -> RiskAssessment:
    command = str(command or "").strip()
    if not command:
        return RiskAssessment(RiskTier.TIER_3, "empty command is invalid", command)
    if _CRITICAL.search(command):
        return RiskAssessment(RiskTier.TIER_3, "critical destructive command pattern", command, ("critical",))
    matches = tuple(match.group(0) for match in _STRUCTURE.finditer(command))
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        return RiskAssessment(RiskTier.TIER_3, "command could not be parsed safely", command, ("parse_failure",))
    if matches:
        return RiskAssessment(RiskTier.TIER_3, "compound shell structure requires explicit approval", command, matches)
    if not tokens:
        return RiskAssessment(RiskTier.TIER_3, "command has no executable", command)
    if tokens[0].lower() in {"cat", "dir", "ls", "pwd", "echo", "type", "git"} and not any(flag in tokens for flag in ("push", "reset", "clean")):
        return RiskAssessment(RiskTier.TIER_2, "command is parsed but may touch external state", command)
    return RiskAssessment(RiskTier.TIER_2, "unknown command requires review", command, (tokens[0],))

"""Fail-closed path, risk, and permission controls."""

from .paths import PathPolicy, PathSecurityError, WorkspaceRoots
from .permissions import (
    PermissionDecision,
    PermissionEffect,
    PermissionGate,
    PermissionMode,
    PermissionRule,
)
from .risk import RiskAssessment, RiskTier, classify_action, classify_command

__all__ = [
    "PathPolicy",
    "PathSecurityError",
    "PermissionDecision",
    "PermissionEffect",
    "PermissionGate",
    "PermissionMode",
    "PermissionRule",
    "RiskAssessment",
    "RiskTier",
    "WorkspaceRoots",
    "classify_action",
    "classify_command",
]

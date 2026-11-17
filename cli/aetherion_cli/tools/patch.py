"""Permission-gated anchor editing tool registration."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..diff.fuzzy import replace_anchor
from ..diff.patch import apply_file_edit, unified_diff
from ..security.paths import PathPolicy
from .registry import ToolRegistry


class ApplyPatchInput(BaseModel):
    path: str
    old_string: str
    new_string: str
    expected_sha256: str | None = None
    replace_all: bool = False


async def _preview_edit(data: ApplyPatchInput, context: Any) -> dict[str, Any]:
    policy = _policy_from_context(context)
    path = policy.assert_writable(data.path)
    original = path.read_text(encoding="utf-8")
    replacement = replace_anchor(original, data.old_string, data.new_string, replace_all=data.replace_all)
    patch = unified_diff(path, original, replacement.text)
    return {"path": str(path), "diff": patch.diff, "user_modified": replacement.user_modified}


async def _apply_edit(data: ApplyPatchInput, context: Any) -> dict[str, Any]:
    policy = _policy_from_context(context)
    path = policy.assert_writable(data.path)
    patch = apply_file_edit(
        path,
        data.old_string,
        data.new_string,
        expected_sha256=data.expected_sha256,
        replace_all=data.replace_all,
    )
    return {"path": patch.path, "diff": patch.diff, "user_modified": patch.user_modified}


def _policy_from_context(context: Any) -> PathPolicy:
    policy = getattr(context, "path_policy", None)
    if not isinstance(policy, PathPolicy):
        raise RuntimeError("patch tool requires a local workspace path policy")
    return policy


def register_patch_tools(registry: ToolRegistry, context: Any) -> None:
    registry.register(
        "apply_patch",
        "Replace one uniquely identified text anchor with atomic verification",
        ApplyPatchInput,
        _apply_edit,
        is_read_only=False,
        is_concurrency_safe=False,
        risk_tier=2,
        preview=_preview_edit,
    )


__all__ = ["ApplyPatchInput", "register_patch_tools"]

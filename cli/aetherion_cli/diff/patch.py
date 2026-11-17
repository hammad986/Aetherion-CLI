"""Unified diff generation and fail-closed atomic file edits."""

from __future__ import annotations

import difflib
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .fuzzy import FuzzyMatchError, replace_anchor


class PatchError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class FilePatch:
    path: str
    original: str
    updated: str
    diff: str
    user_modified: bool = False


def unified_diff(path: str | Path, original: str, updated: str, *, context: int = 3) -> FilePatch:
    if context < 0:
        raise ValueError("context must be non-negative")
    old_lines = original.replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)
    new_lines = updated.replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)
    diff = "".join(difflib.unified_diff(old_lines, new_lines, fromfile=str(path), tofile=str(path), n=context))
    return FilePatch(str(path), original, updated, diff)


def validate_patch_envelope(patch: str) -> None:
    if not patch.startswith("*** Begin Patch") or not patch.rstrip().endswith("*** End Patch"):
        raise PatchError("patch must start with *** Begin Patch and end with *** End Patch")


def apply_file_edit(
    path: str | Path,
    old_string: str,
    new_string: str,
    *,
    expected_sha256: str | None = None,
    replace_all: bool = False,
    encoding: str = "utf-8",
) -> FilePatch:
    target = Path(path)
    if not target.exists() or not target.is_file():
        raise PatchError("target file does not exist")
    original_bytes = target.read_bytes()
    if b"\x00" in original_bytes:
        raise PatchError("binary files cannot be edited")
    actual_hash = hashlib.sha256(original_bytes).hexdigest()
    if expected_sha256 is not None and expected_sha256 != actual_hash:
        raise PatchError("stale read: file content changed before edit")
    bom = original_bytes.startswith(b"\xef\xbb\xbf")
    text = original_bytes.decode("utf-8-sig" if bom else encoding)
    try:
        replacement = replace_anchor(text, old_string, new_string, replace_all=replace_all)
    except FuzzyMatchError as exc:
        raise PatchError(str(exc)) from exc
    if replacement.text == text:
        return FilePatch(str(target), text, text, "", replacement.user_modified)
    patch = unified_diff(target, text, replacement.text)
    encoded = replacement.text.encode("utf-8-sig" if bom else encoding)
    atomic_write(target, encoded)
    post_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    if post_hash != hashlib.sha256(encoded).hexdigest():
        raise PatchError("post-write verification failed")
    return FilePatch(patch.path, patch.original, patch.updated, patch.diff, replacement.user_modified)


def atomic_write(path: str | Path, data: bytes, *, mode: int | None = None) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    original_mode = target.stat().st_mode if target.exists() else None
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp, mode)
        elif original_mode is not None:
            os.chmod(temp, original_mode)
        try:
            os.replace(temp, target)
        except OSError as exc:
            raise PatchError("atomic replacement failed; original file was preserved") from exc
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass

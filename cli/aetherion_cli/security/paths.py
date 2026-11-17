"""Canonical workspace path and filesystem containment checks."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path


class PathSecurityError(PermissionError):
    def __init__(self, code: str, path: str, message: str | None = None) -> None:
        self.code = code
        self.path = path
        super().__init__(message or f"{code}: {path}")


@dataclass(slots=True, frozen=True)
class WorkspaceRoots:
    primary_root: Path
    additional_roots: tuple[Path, ...] = ()
    read_only_roots: tuple[Path, ...] = ()
    writable_roots: tuple[Path, ...] = ()
    temp_root: Path | None = None

    def __post_init__(self) -> None:
        primary = self.primary_root.expanduser().resolve()
        object.__setattr__(self, "primary_root", primary)
        object.__setattr__(self, "additional_roots", tuple(path.expanduser().resolve() for path in self.additional_roots))
        object.__setattr__(self, "read_only_roots", tuple(path.expanduser().resolve() for path in self.read_only_roots))
        object.__setattr__(self, "writable_roots", tuple(path.expanduser().resolve() for path in self.writable_roots))
        if self.temp_root is not None:
            object.__setattr__(self, "temp_root", self.temp_root.expanduser().resolve())

    @property
    def readable(self) -> tuple[Path, ...]:
        return (self.primary_root, *self.additional_roots, *self.read_only_roots, *self.writable_roots)

    @property
    def writable(self) -> tuple[Path, ...]:
        return (self.primary_root, *self.writable_roots, *(tuple([self.temp_root]) if self.temp_root else ()))


_RESERVED = {"CON", "NUL", "PRN", "AUX", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class PathPolicy:
    def __init__(self, roots: WorkspaceRoots, *, reject_symlinks: bool = True) -> None:
        self.roots = roots
        self.reject_symlinks = reject_symlinks

    def resolve(self, candidate: str | os.PathLike[str], *, write: bool = False, allow_missing: bool = True) -> Path:
        raw = os.fspath(candidate)
        self._validate_raw(raw)
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.roots.primary_root / path
        if write:
            roots = self.roots.writable
        else:
            roots = self.roots.readable
        resolved = Path(os.path.realpath(path))
        if not allow_missing and not resolved.exists():
            raise PathSecurityError("not_found", raw)
        if self.reject_symlinks and self._contains_symlink(path):
            raise PathSecurityError("symlink_in_path", raw)
        if not self._contained(resolved, roots):
            raise PathSecurityError("path_not_canonical", raw, "path escapes configured workspace roots")
        if resolved.exists() and not stat.S_ISREG(resolved.stat().st_mode) and not resolved.is_dir():
            raise PathSecurityError("unsupported_name", raw, "special filesystem nodes are not allowed")
        if write and self._contained(resolved, self.roots.read_only_roots):
            raise PathSecurityError("read_only_root", raw)
        return resolved

    def assert_readable(self, candidate: str | os.PathLike[str]) -> Path:
        return self.resolve(candidate, write=False, allow_missing=False)

    def assert_writable(self, candidate: str | os.PathLike[str]) -> Path:
        return self.resolve(candidate, write=True, allow_missing=True)

    @staticmethod
    def _validate_raw(raw: str) -> None:
        if not raw or "\x00" in raw:
            raise PathSecurityError("invalid_path", raw)
        if raw.startswith(("\\\\", "//")):
            raise PathSecurityError("unc_path", raw)
        if re.match(r"^[A-Za-z]:[^\\/]", raw):
            raise PathSecurityError("drive_relative", raw)
        for component in re.split(r"[\\/]", raw):
            if not component or component in {".", ".."}:
                continue
            if component.endswith((".", " ")):
                raise PathSecurityError("unsupported_name", raw)
            stem = component.split(":", 1)[0].split(".", 1)[0].upper()
            if stem in _RESERVED:
                raise PathSecurityError("unsupported_name", raw)
            if ":" in component and not (len(component) == 2 and component[1] == ":"):
                raise PathSecurityError("alternate_data_stream", raw)

    @staticmethod
    def _contains_symlink(path: Path) -> bool:
        current = path
        try:
            parts = list(current.parents)[::-1] + [current]
            # Inspect the lexical path as well as existing targets. A missing
            # child below a symlink must still be rejected.
            return any(part.is_symlink() for part in parts)
        except OSError:
            return True

    @staticmethod
    def _contained(path: Path, roots: tuple[Path, ...]) -> bool:
        if not roots:
            return False
        target = os.path.normcase(str(path))
        for root in roots:
            try:
                if os.path.commonpath([target, os.path.normcase(str(root))]) == os.path.normcase(str(root)):
                    return True
            except ValueError:
                continue
        return False

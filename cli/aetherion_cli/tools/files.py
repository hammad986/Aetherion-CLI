"""Safe file read/write/search/grep tools backed by PathPolicy."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..security.paths import PathPolicy, PathSecurityError, WorkspaceRoots
from ..diff.patch import unified_diff
from .registry import ToolRegistry


class ReadFileInput(BaseModel):
    path: str
    offset: int = Field(default=0, ge=0)
    limit: int | None = Field(default=None, ge=1, le=100_000)


class WriteFileInput(BaseModel):
    path: str
    content: str
    expected_hash: str | None = None


class SearchInput(BaseModel):
    pattern: str = "**/*"
    root: str = "."
    limit: int = Field(default=200, ge=1, le=10_000)


class GrepInput(BaseModel):
    pattern: str
    root: str = "."
    glob: str = "**/*"
    limit: int = Field(default=200, ge=1, le=10_000)


@dataclass(slots=True, frozen=True)
class ReadState:
    path: str
    mtime_ns: int
    size: int
    sha256: str
    encoding: str
    newline: str
    bom: bool


class FileTools:
    def __init__(self, roots: WorkspaceRoots | PathPolicy) -> None:
        self.policy = roots if isinstance(roots, PathPolicy) else PathPolicy(roots)
        self.read_states: dict[str, ReadState] = {}

    async def read_file(self, data: ReadFileInput, _ctx: Any = None) -> dict[str, Any]:
        path = self.policy.assert_readable(data.path)
        raw = path.read_bytes()
        if b"\x00" in raw:
            raise ValueError("binary files are not supported by read_file")
        encoding, bom = _detect_encoding(raw)
        text = raw.decode(encoding)
        newline = "CRLF" if "\r\n" in text else "LF"
        lines = text.splitlines(keepends=True)
        selected = lines[data.offset : data.offset + data.limit if data.limit else None]
        digest = hashlib.sha256(raw).hexdigest()
        stat_result = path.stat()
        self.read_states[str(path)] = ReadState(str(path), stat_result.st_mtime_ns, stat_result.st_size, digest, encoding, newline, bom)
        return {
            "path": str(path),
            "content": "".join(selected),
            "offset": data.offset,
            "line_count": len(lines),
            "sha256": digest,
            "encoding": encoding,
            "newline": newline,
            "bom": bom,
        }

    async def write_file(self, data: WriteFileInput, _ctx: Any = None) -> dict[str, Any]:
        path = self.policy.assert_writable(data.path)
        existing = path.read_bytes() if path.exists() else b""
        if path.exists() and b"\x00" in existing:
            raise ValueError("binary files are not supported by write_file")
        current_hash = hashlib.sha256(existing).hexdigest()
        if data.expected_hash is not None and data.expected_hash != current_hash:
            raise RuntimeError("stale_read: file hash changed since the caller read it")
        state = self.read_states.get(str(path))
        if state is not None and path.exists():
            stat_result = path.stat()
            if state.sha256 != current_hash:
                raise RuntimeError("stale_read: file changed since the current session read it")
            if state.mtime_ns != stat_result.st_mtime_ns or state.size != stat_result.st_size:
                raise RuntimeError("stale_read: file metadata changed since the current session read it")
        encoding, bom = _detect_encoding(existing)
        newline = "\r\n" if b"\r\n" in existing else "\n"
        content = data.content.replace("\r\n", "\n").replace("\r", "\n")
        if newline == "\r\n":
            content = content.replace("\n", "\r\n")
        encoded = content.encode(encoding)
        if bom and not encoded.startswith(b"\xef\xbb\xbf"):
            encoded = b"\xef\xbb\xbf" + encoded
        _atomic_replace(path, encoded)
        digest = hashlib.sha256(encoded).hexdigest()
        stat_result = path.stat()
        self.read_states[str(path)] = ReadState(str(path), stat_result.st_mtime_ns, stat_result.st_size, digest, encoding, newline, bom)
        return {"path": str(path), "sha256": digest, "bytes": len(encoded), "user_modified": False}

    async def preview_write(self, data: WriteFileInput, _ctx: Any = None) -> dict[str, Any]:
        """Create a review diff without changing the target file."""
        path = self.policy.assert_writable(data.path)
        existing = path.read_bytes() if path.exists() else b""
        current_hash = hashlib.sha256(existing).hexdigest()
        if data.expected_hash is not None and data.expected_hash != current_hash:
            raise RuntimeError("stale_read: file hash changed since the caller read it")
        encoding, bom = _detect_encoding(existing)
        original = existing.decode(encoding) if existing else ""
        newline = "\r\n" if b"\r\n" in existing else "\n"
        content = data.content.replace("\r\n", "\n").replace("\r", "\n")
        if newline == "\r\n":
            content = content.replace("\n", "\r\n")
        if bom and not original.startswith("\ufeff"):
            original = "\ufeff" + original
        patch = unified_diff(path, original, content)
        return {"path": str(path), "diff": patch.diff, "expected_hash": current_hash}

    async def search(self, data: SearchInput, _ctx: Any = None) -> dict[str, Any]:
        root = self.policy.assert_readable(data.root)
        matches: list[str] = []
        for path in root.glob(data.pattern):
            if len(matches) >= data.limit:
                break
            try:
                resolved = self.policy.assert_readable(str(path))
            except PathSecurityError:
                continue
            if resolved.is_file():
                matches.append(str(resolved))
        return {"root": str(root), "matches": matches, "truncated": len(matches) >= data.limit}

    async def grep(self, data: GrepInput, _ctx: Any = None) -> dict[str, Any]:
        root = self.policy.assert_readable(data.root)
        regex = re.compile(data.pattern)
        results: list[dict[str, Any]] = []
        for path in root.glob(data.glob):
            if len(results) >= data.limit:
                break
            if not path.is_file():
                continue
            try:
                resolved = self.policy.assert_readable(str(path))
                text = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError, PathSecurityError):
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    results.append({"path": str(resolved), "line": line_number, "text": line})
                    if len(results) >= data.limit:
                        break
        return {"root": str(root), "matches": results, "truncated": len(results) >= data.limit}


def register_file_tools(registry: ToolRegistry, tools: FileTools) -> None:
    registry.register("read_file", "Read a text file", ReadFileInput, tools.read_file, is_read_only=True, is_concurrency_safe=True, risk_tier=1)
    registry.register(
        "write_file",
        "Write a text file atomically",
        WriteFileInput,
        tools.write_file,
        is_read_only=False,
        is_concurrency_safe=False,
        risk_tier=2,
        preview=tools.preview_write,
    )
    registry.register("search", "Find files below a workspace root", SearchInput, tools.search, is_read_only=True, is_concurrency_safe=True, risk_tier=1)
    registry.register("grep", "Search text files with a regular expression", GrepInput, tools.grep, is_read_only=True, is_concurrency_safe=True, risk_tier=1)


def _detect_encoding(raw: bytes) -> tuple[str, bool]:
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", True
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16", True
    return "utf-8", False


def _atomic_replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temp_path, path)
        except OSError as exc:
            raise RuntimeError("atomic replacement failed; original file was preserved") from exc
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass

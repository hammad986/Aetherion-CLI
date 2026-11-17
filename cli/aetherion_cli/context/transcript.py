"""Crash-safe append-only JSONL transcript with resume and fork support."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _encoded_cwd(cwd: str) -> str:
    return cwd.replace("\\", "-").replace("/", "-").replace(":", "-")


class TranscriptRecord(BaseModel):
    """Validated common record envelope; payload fields remain extensible."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type: str
    uuid: str
    parent_uuid: str | None = Field(default=None, alias="parentUuid")
    session_id_camel: str = Field(alias="sessionId")
    session_id: str | None = None
    is_sidechain: bool = Field(default=False, alias="isSidechain")
    timestamp: str
    cwd: str
    version: str
    git_branch: str = Field(alias="gitBranch")
    user_type: str = Field(default="external", alias="userType")
    entrypoint: str | None = None


@dataclass(slots=True, frozen=True)
class BranchLoad:
    records: list[dict[str, Any]]
    leaf_uuid: str | None
    rows_unreadable: tuple[int, ...] = ()
    errors: tuple[str, ...] = ()


def _validate_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an envelope while preserving all payload fields."""

    record = TranscriptRecord.model_validate(dict(raw))
    return record.model_dump(by_alias=True, exclude_none=True)


def _read_rows(path: Path) -> tuple[list[dict[str, Any]], tuple[int, ...], tuple[str, ...]]:
    rows: list[dict[str, Any]] = []
    unreadable: list[int] = []
    errors: list[str] = []
    if not path.exists():
        return rows, (), ()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
                if not isinstance(parsed, Mapping):
                    raise ValueError("record is not an object")
                rows.append(_validate_record(parsed))
            except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as exc:
                unreadable.append(line_number)
                errors.append(f"line {line_number}: {type(exc).__name__}")
    return rows, tuple(unreadable), tuple(errors)


def _find_leaf(rows: Iterable[Mapping[str, Any]]) -> str | None:
    items = list(rows)
    for row in reversed(items):
        if row.get("type") == "last-prompt" and row.get("leafUuid"):
            return str(row["leafUuid"])
    for row in reversed(items):
        if row.get("type") != "last-prompt" and row.get("uuid"):
            return str(row["uuid"])
    return None


def _branch_from_rows(rows: list[dict[str, Any]], leaf_uuid: str | None) -> list[dict[str, Any]]:
    by_uuid = {
        str(row["uuid"]): row
        for row in rows
        if row.get("uuid") and row.get("type") != "last-prompt"
    }
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current = leaf_uuid
    while current and current in by_uuid and current not in seen:
        seen.add(current)
        record = by_uuid[current]
        if record.get("isSidechain"):
            break
        chain.append(record)
        parent = record.get("parentUuid")
        current = str(parent) if parent else None
    return list(reversed(chain))


def load_branch(path: str | Path) -> list[dict[str, Any]]:
    """Load the active parent chain while tolerating malformed JSONL rows."""

    rows, _, _ = _read_rows(Path(path).expanduser())
    return _branch_from_rows(rows, _find_leaf(rows))


class Transcript:
    """One append-only session transcript.

    Every durable record receives a full common envelope. Appends write the
    semantic record and a ``last-prompt`` leaf pointer under one lock, flush and
    fsync both before advancing the in-memory leaf.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
        version: str = "0.1.0",
        git_branch: str | None = None,
        user_type: str = "external",
        entrypoint: str = "aetherion-cli",
    ) -> None:
        self.cwd = cwd or os.getcwd()
        self.session_id = session_id or str(uuid4())
        self.version = version
        self.git_branch = git_branch or os.environ.get("GIT_BRANCH", "HEAD")
        self.user_type = user_type
        self.entrypoint = entrypoint
        self._lock = threading.RLock()
        self.rows: list[dict[str, Any]] = []
        self.leaf: str | None = None
        self.rows_unreadable: tuple[int, ...] = ()
        self.load_errors: tuple[str, ...] = ()

        if path is None:
            root = Path.home() / ".claude" / "projects" / _encoded_cwd(self.cwd)
            path = root / f"{self.session_id}.jsonl"
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load_existing()

    @classmethod
    def resume(cls, path: str | Path, **kwargs: Any) -> Transcript:
        """Open an existing session and recover its last valid leaf."""

        return cls(path, **kwargs)

    @property
    def branch(self) -> list[dict[str, Any]]:
        with self._lock:
            return _branch_from_rows(list(self.rows), self.leaf)

    def _load_existing(self) -> None:
        with self._lock:
            rows, unreadable, errors = _read_rows(self.path)
            self.rows = rows
            self.rows_unreadable = unreadable
            self.load_errors = errors
            if rows:
                first = rows[0]
                self.session_id = str(first.get("sessionId", self.session_id))
                self.cwd = str(first.get("cwd", self.cwd))
                self.version = str(first.get("version", self.version))
                self.git_branch = str(first.get("gitBranch", self.git_branch))
                self.user_type = str(first.get("userType", self.user_type))
            self.leaf = _find_leaf(rows)

    def _envelope(self, type_: str, payload: Mapping[str, Any], parent: str | None) -> dict[str, Any]:
        record: dict[str, Any] = {
            "type": type_,
            "uuid": str(uuid4()),
            "parentUuid": parent,
            "sessionId": self.session_id,
            "session_id": self.session_id,
            "isSidechain": False,
            "timestamp": _utc_now(),
            "cwd": self.cwd,
            "version": self.version,
            "gitBranch": self.git_branch,
            "userType": self.user_type,
            "entrypoint": self.entrypoint,
        }
        for key, value in payload.items():
            if key in {"type", "uuid", "parentUuid", "sessionId", "timestamp"}:
                continue
            record[key] = value
        return _validate_record(record)

    def _leaf_pointer(self, leaf_uuid: str, parent: str | None) -> dict[str, Any]:
        return self._envelope("last-prompt", {"leafUuid": leaf_uuid}, parent)

    def append(self, type_: str, payload: Mapping[str, Any] | None = None, parent: str | None = None) -> dict[str, Any]:
        """Persist one record and update the active leaf only after fsync."""

        with self._lock:
            record = self._envelope(type_, payload or {}, self.leaf if parent is None else parent)
            pointer = self._leaf_pointer(record["uuid"], record["uuid"])
            encoded = (
                json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                + "\n"
                + json.dumps(pointer, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self.rows.extend((record, pointer))
            self.leaf = record["uuid"]
            return dict(record)

    def append_message(self, role: str, content: str | list[dict[str, Any]], **metadata: Any) -> dict[str, Any]:
        return self.append(role, {"message": {"role": role, "content": content}, **metadata})

    def append_boundary(
        self,
        summary: str,
        *,
        mode: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if mode not in {"compact", "microcompact", "reactive"}:
            raise ValueError("boundary mode must be compact, microcompact, or reactive")
        subtype = "microcompact_boundary" if mode == "microcompact" else "compact_boundary"
        return self.append(
            "system",
            {
                "subtype": subtype,
                "content": summary,
                "compactMetadata": {"trigger": "reactive" if mode == "reactive" else "auto", **dict(metadata or {})},
            },
        )

    def fork(
        self,
        *,
        leaf_uuid: str | None = None,
        path: str | Path | None = None,
        session_id: str | None = None,
        keep_parent: bool = True,
    ) -> Transcript:
        """Create a new local session file without rewriting the parent."""

        source_leaf = leaf_uuid or self.leaf
        child_id = session_id or str(uuid4())
        child = Transcript(
            path,
            session_id=child_id,
            cwd=self.cwd,
            version=self.version,
            git_branch=self.git_branch,
            user_type=self.user_type,
            entrypoint=self.entrypoint,
        )
        child.append(
            "fork-context-ref",
            {
                "leafUuid": source_leaf,
                "forkSourceParentUuid": source_leaf,
                "isSidechain": False,
                "forkSourceAlive": True,
                "forkBoundaryAt": _utc_now(),
                "forkParentSessionId": self.session_id,
                "keepParent": keep_parent,
                "hookBased": False,
                "originCwd": self.cwd,
                "sessionPermissionRules": {},
            },
            parent=None,
        )
        return child

    def load(self) -> BranchLoad:
        """Reload from disk and return recovery diagnostics."""

        self._load_existing()
        return BranchLoad(self.branch, self.leaf, self.rows_unreadable, self.load_errors)

"""User-controlled, offline Skills Library.

The packaged ``skills/`` tree is a browseable catalogue, not an installation
directory.  Nothing is copied into the user scope until an explicit install or
custom-create operation occurs.  Skill text is data; this module never imports,
executes, installs, or grants permissions to skill content.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent / "skills"
DEFAULT_LIBRARY_DIR = Path(
    os.environ.get("AETHERION_SKILLS_LIBRARY_DIR", str(Path.home() / ".aetherion" / "skills-library"))
)
STATE_VERSION = 1
MAX_SKILL_BYTES = 512 * 1024
MAX_INSTRUCTION_CHARS = 200_000
MAX_DESCRIPTION_CHARS = 2_000
MAX_NAME_CHARS = 128
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\x80-\x9f]")


class SkillLibraryError(ValueError):
    """Raised when a skill-library operation cannot safely complete."""


def slugify_skill_id(name: str) -> str:
    """Return a bounded stable ID; never interpret the input as a path."""
    if not isinstance(name, str):
        return ""
    value = name.strip().lower()
    value = "".join(ch if ord(ch) < 128 else "-" for ch in value)
    value = re.sub(r"[^a-z0-9_-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-_")[:64].rstrip("-_")
    return value if _ID_RE.fullmatch(value) else ""


def _validate_text(
    value: Any,
    field: str,
    maximum: int,
    *,
    required: bool = False,
    single_line: bool = False,
) -> str:
    if not isinstance(value, str):
        raise SkillLibraryError(f"{field} must be text")
    if required and not value.strip():
        raise SkillLibraryError(f"{field} is required")
    if single_line and any(char in value for char in "\r\n"):
        raise SkillLibraryError(f"{field} must be a single line")
    if _CONTROL_RE.search(value.replace("\n", "").replace("\r", "").replace("\t", "")):
        raise SkillLibraryError(f"{field} contains unsupported control characters")
    if len(value) > maximum:
        raise SkillLibraryError(f"{field} exceeds its size limit")
    return value


def _assert_safe_path(path: Path) -> None:
    """Reject symlink/reparse components in the user-owned library boundary."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = absolute
    while True:
        if current.exists() or current.is_symlink():
            try:
                if current.is_symlink() or os.path.islink(os.fspath(current)):
                    raise SkillLibraryError("skill-library path contains a symlink or reparse point")
            except OSError as exc:
                raise SkillLibraryError("skill-library path cannot be validated") from exc
        if current.parent == current:
            return
        current = current.parent


def _safe_child(root: Path, skill_id: str) -> Path:
    if not _ID_RE.fullmatch(skill_id):
        raise SkillLibraryError("invalid canonical skill ID")
    child = root / skill_id
    _assert_safe_path(root)
    _assert_safe_path(child)
    return child


def _atomic_write(path: Path, data: bytes) -> None:
    _assert_safe_path(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_path(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=os.fspath(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, os.fspath(path))
    except OSError as exc:
        raise SkillLibraryError(f"skill-library write failed: {exc}") from exc
    finally:
        try:
            Path(temp_name).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SkillLibraryError(f"skill-library temporary cleanup failed: {exc}") from exc


def _parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    if not content.startswith("---\n") and not content.startswith("---\r\n"):
        return {}, content
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, content
    end_index = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end_index is None:
        return {}, content
    fields: dict[str, str] = {}
    current_list: Optional[str] = None
    for raw_line in lines[1:end_index]:
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if line.startswith("  -") and current_list:
            fields[current_list] = f"{fields.get(current_list, '')}\n{line[3:].strip()}"
            continue
        if ":" not in line:
            current_list = None
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        fields[key] = value
        current_list = key if not value else None
    return fields, "".join(lines[end_index + 1:]).lstrip("\r\n")


def _list_value(value: str) -> list[str]:
    if not value:
        return []
    return [item.strip().strip("'\"") for item in value.splitlines() if item.strip()]


def _requirements(content: str) -> list[str]:
    patterns = (
        r"(?im)^\s*(?:requires?|dependencies?|prerequisites?)\s*:\s*(.+)$",
        r"(?im)\b(?:pip install|npm install|brew install|requires?\s+)([^\n`]+)",
        r"(?im)\b([A-Z][A-Z0-9_]{2,})\b",
    )
    found: list[str] = []
    for pattern in patterns:
        for match in re.findall(pattern, content):
            value = match if isinstance(match, str) else " ".join(match)
            value = value.strip(" .,:;`[]()")
            if value and value not in found and len(value) <= 160:
                found.append(value)
            if len(found) >= 12:
                return found
    return found


def _safe_read(path: Path) -> str:
    _assert_safe_path(path)
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_SKILL_BYTES + 1)
    except OSError as exc:
        raise SkillLibraryError(f"skill source cannot be read: {exc}") from exc
    if len(data) > MAX_SKILL_BYTES:
        raise SkillLibraryError("skill source exceeds the 512 KiB limit")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillLibraryError("skill source is not valid UTF-8") from exc


@dataclass(frozen=True)
class SkillEntry:
    skill_id: str
    name: str
    description: str
    version: str
    category: str
    source_kind: str
    source_path: str
    requirements: tuple[str, ...]
    readiness: str
    runtime_connected: bool
    installed: bool
    enabled: bool
    custom: bool
    content_sha256: str
    content: Optional[str] = None

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "category": self.category,
            "source_kind": self.source_kind,
            "source_path": self.source_path,
            "requirements": list(self.requirements),
            "readiness": self.readiness,
            "runtime_connected": self.runtime_connected,
            "installed": self.installed,
            "enabled": self.enabled,
            "custom": self.custom,
            "content_sha256": self.content_sha256,
        }
        if include_content:
            result["content"] = self.content or ""
        return result


class SkillLibrary:
    """Catalogue plus user-scoped installed/custom skill state."""

    _state_lock = threading.RLock()

    def __init__(self, root: Optional[Path] = None, builtin_root: Optional[Path] = None) -> None:
        self.root = Path(root or DEFAULT_LIBRARY_DIR).expanduser()
        self.builtin_root = Path(builtin_root or BUILTIN_SKILLS_DIR).resolve()
        self.installed_root = self.root / "installed"
        self.custom_root = self.root / "custom"
        self.state_path = self.root / "state.json"
        _assert_safe_path(self.root)

    def _state(self) -> dict[str, Any]:
        with self._state_lock:
            if not self.state_path.exists():
                return {"version": STATE_VERSION, "installed": {}, "enabled": []}
            content = _safe_read(self.state_path)
            try:
                value = json.loads(content)
            except json.JSONDecodeError as exc:
                raise SkillLibraryError("skill-library state is malformed") from exc
            if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
                raise SkillLibraryError("skill-library state has an unsupported schema")
            if not isinstance(value.get("installed"), dict) or not isinstance(value.get("enabled"), list):
                raise SkillLibraryError("skill-library state has invalid installation fields")
            return value

    def _write_state(self, value: Mapping[str, Any]) -> None:
        with self._state_lock:
            _atomic_write(self.state_path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))

    @staticmethod
    def _metadata(skill_id: str, path: Path, content: str, *, source_kind: str, category: str, custom: bool) -> SkillEntry:
        fields, body = _parse_frontmatter(content)
        name = fields.get("name") or skill_id.replace("-", " ").title()
        description = fields.get("description", "")
        requirements = tuple(_requirements(content))
        return SkillEntry(
            skill_id=skill_id,
            name=name,
            description=description,
            version=fields.get("version", "1.0.0"),
            category=category,
            source_kind=source_kind,
            source_path=str(path),
            requirements=requirements,
            readiness="definition_only_not_runtime_wired",
            runtime_connected=False,
            installed=False,
            enabled=False,
            custom=custom,
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            content=content,
        )

    def _builtin_entries(self) -> list[SkillEntry]:
        if not self.builtin_root.is_dir():
            return []
        entries: list[SkillEntry] = []
        seen: set[str] = set()
        for path in sorted(self.builtin_root.rglob("SKILL.md")):
            content = _safe_read(path)
            fields, _ = _parse_frontmatter(content)
            skill_id = slugify_skill_id(fields.get("name") or path.parent.name)
            if not skill_id or skill_id in seen:
                continue
            seen.add(skill_id)
            relative = path.relative_to(self.builtin_root)
            category = relative.parts[0] if len(relative.parts) > 1 else "uncategorized"
            entries.append(self._metadata(skill_id, path, content, source_kind="bundled", category=category, custom=False))
        return entries

    def _custom_entries(self) -> list[SkillEntry]:
        if not self.custom_root.is_dir():
            return []
        entries: list[SkillEntry] = []
        for directory in sorted(self.custom_root.iterdir()):
            if not directory.is_dir() or not _ID_RE.fullmatch(directory.name):
                continue
            path = directory / "SKILL.md"
            if path.is_file():
                content = _safe_read(path)
                entries.append(self._metadata(directory.name, path, content, source_kind="custom", category="custom", custom=True))
        return entries

    def catalogue(self, *, query: str = "", category: str = "", installed_only: bool = False, custom_only: bool = False) -> list[SkillEntry]:
        state = self._state()
        installed = state["installed"]
        enabled = set(state["enabled"])
        entries = self._builtin_entries() + self._custom_entries()
        result: list[SkillEntry] = []
        needle = query.strip().lower()
        for entry in entries:
            if custom_only and not entry.custom:
                continue
            if category and entry.category != category:
                continue
            if needle and needle not in " ".join((entry.skill_id, entry.name, entry.description, entry.category)).lower():
                continue
            installed_path: Optional[Path] = None
            installed_value = False
            if entry.custom:
                if entry.skill_id in installed:
                    metadata = installed.get(entry.skill_id) or {}
                    installed_path = Path(metadata.get("path") or entry.source_path)
                    expected_path = (self.custom_root / entry.skill_id / "SKILL.md").resolve()
                    if installed_path.resolve(strict=False) != expected_path:
                        raise SkillLibraryError(f"installed skill '{entry.skill_id}' is outside the custom library boundary")
                    _assert_safe_path(installed_path)
                    if installed_path.is_file():
                        installed_content = _safe_read(installed_path)
                        digest = hashlib.sha256(installed_content.encode("utf-8")).hexdigest()
                        expected = str(metadata.get("sha256") or "")
                        if expected and expected != digest:
                            raise SkillLibraryError(f"installed skill '{entry.skill_id}' failed integrity validation")
                        installed_value = True
                        entry = replace(entry, source_path=str(installed_path), content_sha256=digest)
            elif entry.skill_id in installed:
                metadata = installed.get(entry.skill_id) or {}
                installed_path = Path(metadata.get("path") or (self.installed_root / entry.skill_id / "SKILL.md"))
                expected_path = (self.installed_root / entry.skill_id / "SKILL.md").resolve()
                if installed_path.resolve(strict=False) != expected_path:
                    raise SkillLibraryError(f"installed skill '{entry.skill_id}' is outside the installed library boundary")
                _assert_safe_path(installed_path)
                if installed_path.is_file():
                    installed_content = _safe_read(installed_path)
                    digest = hashlib.sha256(installed_content.encode("utf-8")).hexdigest()
                    expected = str(metadata.get("sha256") or "")
                    if expected and expected != digest:
                        raise SkillLibraryError(f"installed skill '{entry.skill_id}' failed integrity validation")
                    installed_value = True
                    entry = replace(
                        entry,
                        source_path=str(installed_path),
                        content_sha256=digest,
                    )
            enabled_value = installed_value and entry.skill_id in enabled
            if installed_only and not installed_value:
                continue
            result.append(SkillEntry(**{**entry.__dict__, "installed": installed_value, "enabled": enabled_value, "content": None}))
        return result

    def get(self, skill_id: str, *, include_content: bool = True) -> Optional[SkillEntry]:
        for entry in self.catalogue():
            if entry.skill_id == skill_id:
                if include_content:
                    content = _safe_read(Path(entry.source_path))
                    return SkillEntry(**{**entry.__dict__, "content": content})
                return entry
        return None

    def _require_entry(self, skill_id: str) -> SkillEntry:
        entry = self.get(skill_id)
        if entry is None:
            raise SkillLibraryError(f"skill '{skill_id}' was not found")
        return entry

    def install(self, skill_id: str) -> SkillEntry:
        with self._state_lock:
            entry = self._require_entry(skill_id)
            source = Path(entry.source_path)
            content = _safe_read(source)
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            state = self._state()
            if entry.custom:
                state["installed"][skill_id] = {
                    "source_kind": "custom",
                    "path": str(source),
                    "sha256": digest,
                    "version": entry.version,
                }
            else:
                destination = _safe_child(self.installed_root, skill_id)
                if destination.exists():
                    _assert_safe_path(destination)
                    shutil.rmtree(destination)
                destination.mkdir(parents=True, exist_ok=True)
                _atomic_write(destination / "SKILL.md", content.encode("utf-8"))
                state["installed"][skill_id] = {
                    "source_kind": "bundled",
                    "path": str(destination / "SKILL.md"),
                    "sha256": digest,
                    "version": entry.version,
                }
            self._write_state(state)
            return self.get(skill_id)  # type: ignore[return-value]

    def uninstall(self, skill_id: str) -> bool:
        with self._state_lock:
            entry = self._require_entry(skill_id)
            state = self._state()
            if not entry.custom:
                destination = _safe_child(self.installed_root, skill_id)
                if destination.exists():
                    _assert_safe_path(destination)
                    shutil.rmtree(destination)
            state["installed"].pop(skill_id, None)
            state["enabled"] = [item for item in state["enabled"] if item != skill_id]
            self._write_state(state)
            return True

    def set_enabled(self, skill_id: str, enabled: bool) -> SkillEntry:
        with self._state_lock:
            entry = self._require_entry(skill_id)
            state = self._state()
            installed = entry.installed
            if not installed:
                raise SkillLibraryError("install the skill before enabling it")
            active = set(state["enabled"])
            if enabled:
                active.add(skill_id)
            else:
                active.discard(skill_id)
            state["enabled"] = sorted(active)
            self._write_state(state)
            return self.get(skill_id)  # type: ignore[return-value]

    def create_custom(self, name: str, description: str, instructions: str) -> SkillEntry:
        with self._state_lock:
            skill_id = slugify_skill_id(name)
            if not skill_id:
                raise SkillLibraryError("name must produce a safe ID of at least 3 characters")
            description = _validate_text(
                description or "", "description", MAX_DESCRIPTION_CHARS, single_line=True
            )
            instructions = _validate_text(instructions, "instructions", MAX_INSTRUCTION_CHARS, required=True)
            if self.get(skill_id) is not None:
                raise SkillLibraryError(f"skill ID '{skill_id}' already exists")
            path = _safe_child(self.custom_root, skill_id) / "SKILL.md"
            content = "---\n" + f"name: {skill_id}\n" + f"description: {description}\n" + "version: 1.0.0\n---\n\n" + instructions.strip() + "\n"
            _atomic_write(path, content.encode("utf-8"))
            state = self._state()
            state["installed"][skill_id] = {
                "source_kind": "custom",
                "path": str(path),
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "version": "1.0.0",
            }
            self._write_state(state)
            return self.get(skill_id)  # type: ignore[return-value]

    def edit_custom(self, skill_id: str, description: str, instructions: str) -> SkillEntry:
        with self._state_lock:
            entry = self._require_entry(skill_id)
            if not entry.custom:
                raise SkillLibraryError("bundled skill definitions cannot be edited")
            description = _validate_text(
                description or "", "description", MAX_DESCRIPTION_CHARS, single_line=True
            )
            instructions = _validate_text(instructions, "instructions", MAX_INSTRUCTION_CHARS, required=True)
            path = _safe_child(self.custom_root, skill_id) / "SKILL.md"
            content = "---\n" + f"name: {skill_id}\n" + f"description: {description}\n" + f"version: {entry.version}\n---\n\n" + instructions.strip() + "\n"
            _atomic_write(path, content.encode("utf-8"))
            state = self._state()
            if skill_id in state["installed"]:
                state["installed"][skill_id].update({
                    "path": str(path),
                    "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                })
                self._write_state(state)
            return self.get(skill_id)  # type: ignore[return-value]

    def delete_custom(self, skill_id: str) -> bool:
        with self._state_lock:
            entry = self._require_entry(skill_id)
            if not entry.custom:
                raise SkillLibraryError("only custom skills can be deleted")
            path = _safe_child(self.custom_root, skill_id)
            if path.exists():
                _assert_safe_path(path)
                shutil.rmtree(path)
            state = self._state()
            state["installed"].pop(skill_id, None)
            state["enabled"] = [item for item in state["enabled"] if item != skill_id]
            self._write_state(state)
            return True

    def export(self, skill_id: str) -> tuple[str, bytes, str]:
        entry = self._require_entry(skill_id)
        content = _safe_read(Path(entry.source_path))
        filename = f"{skill_id}.SKILL.md"
        return filename, content.encode("utf-8"), "text/markdown; charset=utf-8"

    def export_to(self, skill_id: str, destination: Path, *, force: bool = False) -> Path:
        """Atomically export a definition without following symlink destinations."""
        _assert_safe_path(destination.parent)
        if destination.is_symlink():
            raise SkillLibraryError("export destination is a symlink")
        if destination.exists() and destination.is_dir():
            raise SkillLibraryError("export destination is a directory")
        if destination.exists() and not force:
            raise SkillLibraryError(f"export destination already exists: {destination} (use --force)")
        _, content, _ = self.export(skill_id)
        _atomic_write(destination, content)
        return destination

    def explicit_runtime_selection(self, skill_ids: Iterable[str]) -> list[SkillEntry]:
        """Return only explicitly requested installed/enabled definitions."""
        selected: list[SkillEntry] = []
        for skill_id in skill_ids:
            entry = self.get(str(skill_id), include_content=True)
            if entry is None or not entry.installed or not entry.enabled:
                continue
            selected.append(entry)
        return selected

    def inventory_summary(self) -> dict[str, int]:
        entries = self.catalogue()
        return {
            "available": len(entries),
            "installed": sum(entry.installed for entry in entries),
            "enabled": sum(entry.enabled for entry in entries),
            "custom": sum(entry.custom for entry in entries),
            "runtime_connected": sum(entry.runtime_connected for entry in entries),
            "definition_only": sum(entry.readiness == "definition_only_not_runtime_wired" for entry in entries),
        }

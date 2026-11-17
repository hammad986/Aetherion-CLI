"""Safe, metadata-only GitHub plugin management for the Aetherion CLI.

This module deliberately does not import, install, or execute plugin code.  It
only downloads a GitHub archive, validates its metadata and archive structure,
and stores the source in a quarantined directory under ``~/.aetherion``.

The existing backend extension manager owns executable plugin lifecycle
operations.  The CLI uses this separate manager for the control-plane plugin
catalog so that ``plugins install`` cannot accidentally enter that lifecycle.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import unicodedata
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple
from urllib.parse import quote, quote_plus, unquote, urlsplit

import httpx

from . import config as cfg
from .catalog_distribution import CatalogTransport, SignedCatalogService
from .publisher_evidence import (
    GithubCommitSignatureAdapter,
    PublisherEvidenceAdapter,
    PublisherEvidenceError,
    PublisherEvidenceResult,
    PublisherEvidenceStore,
    PublisherEvidenceSubject,
)
from .trust_store import load_catalog_key_provider
from .trust_verifier import CatalogKeyProvider, CatalogSubject, trust_fields_for_record


class PluginManagerError(ValueError):
    """Raised for invalid sources, unsafe archives, or lifecycle failures."""


# REA's verified upstream archive is 15.8 MiB compressed, 4,561 entries, and
# 53.7 MiB uncompressed. These bounded limits admit that source while keeping
# arbitrary downloads finite and substantially below an unbounded extraction.
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_FILES = 5_000
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024
MAX_METADATA_BYTES = 512 * 1024
MAX_TEXT_BYTES = 16 * 1024
DEFAULT_REF = "main"
CATALOG_VERSION = 1
SUPPORTED_PLUGIN_API_MAJOR = 1
GITHUB_HOSTS = {"github.com", "www.github.com"}
_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{2,63}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_PLUGIN_API_VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,2}$")
_MANIFEST_SCHEMA_VERSION_RE = re.compile(r"^1(?:\.0+){0,2}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,256}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PROTECTED_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
}
_PROTECTED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


@dataclass(frozen=True)
class CatalogEntry:
    """A verified, human-curated source that can be selected by short ID."""

    id: str
    name: str
    description: str
    category: str
    source_url: str
    version: str
    author: str
    license: str
    runtime: str
    package_name: str
    verified_commit: str
    homepage: str
    documentation: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "source_url": self.source_url,
            "version": self.version,
            "author": self.author,
            "license": self.license,
            "runtime": self.runtime,
            "package_name": self.package_name,
            "verified_commit": self.verified_commit,
            "homepage": self.homepage,
            "documentation": self.documentation,
            "curated": True,
            "source_type": "curated",
            "catalog_origin": "curated",
            "execution_enabled": False,
            "integrity_status": "unknown",
            "catalog_auth_status": "unsigned",
            "publisher_auth_status": "not_checked",
            "policy_status": "unreviewed",
            "execution_status": "disabled",
            "trust_reason_code": None,
            "catalog_key_id": None,
        }


# These tools are represented as curated metadata entries.  The manager does
# not run npm, npx, a package binary, an MCP server, or any repository
# lifecycle hook.
CURATED_CATALOG: Tuple[CatalogEntry, ...] = (
    CatalogEntry(
        id="rea",
        name="REA Agents",
        description="Reproducible agent workflow runtime from the verified REA repository.",
        category="agent-runtime",
        source_url="https://github.com/morluto/rea",
        version="6.5.0",
        author="morluto",
        license="MIT",
        runtime="node",
        package_name="rea-agents",
        verified_commit="4e14452ddbf4642235c8a68435f72837510b2075",
        homepage="https://github.com/morluto/rea",
        documentation="https://github.com/morluto/rea#readme",
    ),
    CatalogEntry(
        id="repomix",
        name="Repomix",
        description="Pack a repository into a concise, AI-friendly context file for code analysis.",
        category="repository-intelligence",
        source_url="https://github.com/yamadashy/repomix",
        version="1.10.0",
        author="yamadashy",
        license="MIT",
        runtime="node",
        package_name="repomix",
        verified_commit="e696384d062f08f0246ffd2580d5a206484fc0a4",
        homepage="https://repomix.com",
        documentation="https://repomix.com/guide/",
    ),
    CatalogEntry(
        id="browser-use",
        name="Browser Use",
        description="Browser automation toolkit for agents that need to navigate and operate web pages.",
        category="browser-automation",
        source_url="https://github.com/browser-use/browser-use",
        version="0.1.0",
        author="browser-use",
        license="MIT",
        runtime="python",
        package_name="browser-use",
        verified_commit="aa093a843da5edbf323c0b4145c9b6a0db3ecde8",
        homepage="https://browser-use.com",
        documentation="https://docs.browser-use.com",
    ),
    CatalogEntry(
        id="mcp-filesystem",
        name="MCP Filesystem",
        description="Model Context Protocol filesystem server for explicitly allowed local directories.",
        category="mcp-server",
        source_url="https://github.com/modelcontextprotocol/servers",
        version="2.0.0",
        author="modelcontextprotocol",
        license="MIT",
        runtime="node",
        package_name="@modelcontextprotocol/server-filesystem",
        verified_commit="51b6382a3e827aceb6bce430b1208d69b93d95ea",
        homepage="https://github.com/modelcontextprotocol/servers",
        documentation="https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
    ),
    CatalogEntry(
        id="mcp-github",
        name="MCP GitHub",
        description="Model Context Protocol GitHub server for repository and issue workflows.",
        category="mcp-server",
        source_url="https://github.com/modelcontextprotocol/servers",
        version="2.0.0",
        author="modelcontextprotocol",
        license="MIT",
        runtime="node",
        package_name="@modelcontextprotocol/server-github",
        verified_commit="51b6382a3e827aceb6bce430b1208d69b93d95ea",
        homepage="https://github.com/modelcontextprotocol/servers",
        documentation="https://github.com/modelcontextprotocol/servers/tree/main/src/github",
    ),
    CatalogEntry(
        id="ast-grep",
        name="ast-grep",
        description="Structural code search and rewriting with syntax-aware AST patterns.",
        category="code-analysis",
        source_url="https://github.com/ast-grep/ast-grep",
        version="0.40.5",
        author="ast-grep",
        license="MIT",
        runtime="rust",
        package_name="ast-grep",
        verified_commit="14934d690a547abce8576baff9a1856a10ae7da8",
        homepage="https://ast-grep.github.io",
        documentation="https://ast-grep.github.io/guide/",
    ),
    CatalogEntry(
        id="ripgrep-all",
        name="ripgrep-all",
        description="Search text across PDFs, archives, office documents, and other extracted formats.",
        category="search",
        source_url="https://github.com/phiresky/ripgrep-all",
        version="0.10.6",
        author="phiresky",
        license="AGPL-3.0-only",
        runtime="rust",
        package_name="ripgrep-all",
        verified_commit="fce1a28683142b3dbd6ea2f7b719d89c90aee227",
        homepage="https://github.com/phiresky/ripgrep-all",
        documentation="https://github.com/phiresky/ripgrep-all#readme",
    ),
    CatalogEntry(
        id="trufflehog",
        name="TruffleHog",
        description="Scan repositories and data sources for exposed secrets and credentials.",
        category="security",
        source_url="https://github.com/trufflesecurity/trufflehog",
        version="3.90.8",
        author="Truffle Security",
        license="AGPL-3.0-only",
        runtime="go",
        package_name="trufflehog",
        verified_commit="048de9ff07098d943136cd5231a0e93fbb3ea443",
        homepage="https://trufflesecurity.com",
        documentation="https://github.com/trufflesecurity/trufflehog#readme",
    ),
    CatalogEntry(
        id="code-interpreter",
        name="Code Interpreter",
        description="Sandboxed agent runner with a container image, dispatch CLI, and egress allowlist.",
        category="code-execution",
        source_url="https://github.com/dryvist/nix-agent-sandbox",
        version="1.5.4",
        author="dryvist",
        license="Not declared",
        runtime="shell",
        package_name="nix-agent-sandbox",
        verified_commit="b55bbff3795b4481bef496b9e2cd8ff5013ae7d5",
        homepage="https://github.com/dryvist/nix-agent-sandbox",
        documentation="https://github.com/dryvist/nix-agent-sandbox#readme",
    ),
    CatalogEntry(
        id="aider-tools",
        name="Aider Tools",
        description="Git-aware pair programming assistant for making focused changes in a codebase.",
        category="coding-assistant",
        source_url="https://github.com/Aider-AI/aider",
        version="0.86.2",
        author="Paul Gauthier",
        license="Apache-2.0",
        runtime="python",
        package_name="aider-chat",
        verified_commit="5dc9490bb35f9729ef2c95d00a19ccd30c26339c",
        homepage="https://aider.chat",
        documentation="https://aider.chat/docs/",
    ),
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    with os.fdopen(descriptor, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _strict_json_loads(value: str | bytes) -> Any:
    """Parse manager-controlled and downloaded metadata without JSON ambiguity."""
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = item
        return result

    return json.loads(
        value,
        object_pairs_hook=reject_duplicates,
        parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(f"unsupported JSON constant: {constant}")),
    )


def _clean_text(value: Any, field: str, maximum: int = MAX_TEXT_BYTES) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    if _CONTROL_RE.search(value):
        raise PluginManagerError(f"Manifest field '{field}' contains control characters.")
    if len(value.encode("utf-8")) > maximum:
        raise PluginManagerError(f"Manifest field '{field}' exceeds its size limit.")
    return value.strip()


def _safe_count(value: Any, field: str) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise PluginManagerError(f"GitHub metadata field '{field}' is not an integer.") from exc
    if number < 0:
        raise PluginManagerError(f"GitHub metadata field '{field}' cannot be negative.")
    return number


def _safe_id(value: str, field: str = "id") -> str:
    if not _ID_RE.fullmatch(value):
        raise PluginManagerError(
            f"Invalid plugin {field} '{value}'. Use 3-64 letters, numbers, '_' or '-'."
        )
    return value


def _derived_id(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-_").lower()
    if len(value) < 3:
        value = f"plugin-{value}"
    return _safe_id(value[:64])


def canonicalize_github_url(source: str) -> str:
    """Return a canonical GitHub repository URL or reject it.

    Only a repository root is accepted.  Tree/blob/raw URLs, credentials,
    query strings, fragments, ports, and non-HTTPS schemes are rejected.
    """
    if not isinstance(source, str):
        raise PluginManagerError("GitHub source must be a string.")
    source = source.strip()
    if not source or _CONTROL_RE.search(source) or any(ch.isspace() for ch in source):
        raise PluginManagerError("GitHub source must not contain whitespace or control characters.")

    parsed = urlsplit(source)
    if parsed.scheme.lower() != "https":
        raise PluginManagerError("Only HTTPS GitHub repository URLs are supported.")
    if parsed.hostname is None or parsed.hostname.lower() not in GITHUB_HOSTS:
        raise PluginManagerError("Plugin sources must use github.com.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise PluginManagerError("GitHub source URL contains an invalid port.") from exc
    if parsed.username or parsed.password or port is not None:
        raise PluginManagerError("GitHub source URLs cannot contain credentials or ports.")
    if parsed.query or parsed.fragment:
        raise PluginManagerError("GitHub repository URLs cannot contain a query or fragment.")

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise PluginManagerError("Use a GitHub repository root URL: https://github.com/owner/repository.")
    owner, repository = parts
    if any(_CONTROL_RE.search(part) for part in parts):
        raise PluginManagerError("GitHub owner and repository cannot contain control characters.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", owner):
        raise PluginManagerError("Invalid GitHub owner.")
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", repository):
        raise PluginManagerError("Invalid GitHub repository name.")
    return f"https://github.com/{owner}/{repository}"


def validate_ref(ref: Optional[str]) -> str:
    """Validate a branch, tag, or full commit ref without invoking Git."""
    value = (ref or DEFAULT_REF).strip()
    if not value or _CONTROL_RE.search(value) or not _REF_RE.fullmatch(value):
        raise PluginManagerError("Invalid Git ref. Use a branch, tag, or full 40-character commit SHA.")
    if value.startswith((".", "/", "-")) or value.endswith((".", "/")):
        raise PluginManagerError("Invalid Git ref boundary.")
    if any(token in value for token in ("..", "//", "@{")):
        raise PluginManagerError("Invalid Git ref traversal sequence.")
    if any(char in value for char in ("~", "^", ":", "?", "*", "[", "\\")):
        raise PluginManagerError("Invalid Git ref character.")
    return value


def _relative_file(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise PluginManagerError("Plugin path escaped its managed directory.") from exc


def _atomic_json_write(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_bytes(_canonical_json(data) + b"\n")
        os.replace(str(temp), str(path))
    finally:
        if temp.exists():
            temp.unlink()


def _is_link_or_reparse(path: Path) -> bool:
    """Return True for symlinks and Windows junction/reparse points."""
    try:
        is_junction = getattr(os.path, "isjunction", lambda value: False)
        return path.is_symlink() or os.path.islink(str(path)) or bool(is_junction(str(path)))
    except OSError:
        # An inaccessible path must not be treated as a safe regular path.
        return True


class PluginCatalogManager:
    """Manage quarantined GitHub plugin sources without enabling execution."""

    def __init__(
        self,
        root: Optional[Path] = None,
        json_fetcher: Optional[Callable[[str], Mapping[str, Any]]] = None,
        bytes_fetcher: Optional[Callable[[str], bytes]] = None,
        catalog_endpoint: Optional[str] = None,
        catalog_key_provider: Optional[CatalogKeyProvider] = None,
        catalog_transport: Optional[CatalogTransport] = None,
        catalog_service: Optional[SignedCatalogService] = None,
        catalog_clock: Optional[Callable[[], Any]] = None,
        publisher_evidence_adapter: Optional[PublisherEvidenceAdapter] = None,
        publisher_evidence_store: Optional[PublisherEvidenceStore] = None,
        evidence_only: bool = False,
    ) -> None:
        raw_root = Path(root or (cfg.CONFIG_DIR / "plugins")).expanduser()
        if raw_root.exists() and _is_link_or_reparse(raw_root):
            raise PluginManagerError("Managed plugin root cannot be a symlink or reparse point.")
        self.evidence_only = evidence_only
        self.root = raw_root.absolute() if evidence_only else raw_root.resolve()
        self.staging_dir = self.root / ".staging"
        self.registry_path = self.root / "registry.json"
        self.catalog_path = self.root / "catalog.json"
        self.lock_path = self.root / ".manager.lock"
        self.journal_path = self.root / ".transaction.json"
        if not self.evidence_only:
            self.root.mkdir(parents=True, exist_ok=True)
            if _is_link_or_reparse(self.root):
                raise PluginManagerError("Managed plugin root cannot be a symlink or reparse point.")
            for control_file in (self.registry_path, self.catalog_path, self.journal_path, self.lock_path):
                if _is_link_or_reparse(control_file):
                    raise PluginManagerError("Plugin manager control files cannot be symlinks or reparse points.")
            if self.staging_dir.exists() and _is_link_or_reparse(self.staging_dir):
                raise PluginManagerError("Managed plugin staging directory cannot be a symlink or reparse point.")
            self.staging_dir.mkdir(parents=True, exist_ok=True)
            if not self.staging_dir.is_dir() or _is_link_or_reparse(self.staging_dir):
                raise PluginManagerError("Managed plugin staging path is not a regular directory.")
        self._json_fetcher = json_fetcher or self._fetch_json
        self._bytes_fetcher = bytes_fetcher or self._fetch_bytes
        self.publisher_evidence_adapter = publisher_evidence_adapter or GithubCommitSignatureAdapter(clock=catalog_clock)
        self.publisher_evidence_store = publisher_evidence_store or PublisherEvidenceStore(self.root, clock=catalog_clock)
        if catalog_service is not None:
            self.catalog_service = catalog_service
        elif self.evidence_only:
            self.catalog_service = None
        else:
            endpoint = cfg.get_catalog_url() if catalog_endpoint is None else catalog_endpoint
            provider = catalog_key_provider or load_catalog_key_provider(
                cfg.get_catalog_trust_root(),
                cfg.get_catalog_keyset(),
                cfg.get_catalog_trust_state(),
                clock=catalog_clock,
            )
            self.catalog_service = SignedCatalogService(
                self.root,
                endpoint,
                provider,
                transport=catalog_transport,
                clock=catalog_clock,
            )
        if not self.evidence_only:
            with self._operation_lock():
                self._recover_transaction()

    @contextmanager
    def _operation_lock(self) -> Iterator[None]:
        """Serialize lifecycle mutations across CLI processes and TUI threads."""
        self.root.mkdir(parents=True, exist_ok=True)
        if _is_link_or_reparse(self.lock_path):
            raise PluginManagerError("Plugin manager lock cannot be a symlink or reparse point.")
        with self.lock_path.open("a+b") as handle:
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _managed_staging_path(self, relative: str) -> Path:
        raw_path = self.root / relative
        if _is_link_or_reparse(raw_path):
            raise PluginManagerError("Transaction path cannot be a symlink or reparse point.")
        path = raw_path.resolve(strict=False)
        try:
            path.relative_to(self.root.resolve())
            path.relative_to(self.staging_dir.resolve())
        except ValueError as exc:
            raise PluginManagerError("Transaction path escaped the managed staging directory.") from exc
        return path

    def _managed_plugin_path(self, plugin_id: str) -> Path:
        plugin_id = _safe_id(plugin_id)
        raw_path = self.root / plugin_id
        if _is_link_or_reparse(raw_path):
            raise PluginManagerError(f"Managed plugin path '{plugin_id}' cannot be a symlink or reparse point.")
        resolved = raw_path.resolve(strict=False)
        try:
            resolved.relative_to(self.root.resolve())
        except ValueError as exc:
            raise PluginManagerError("Plugin path escaped the managed plugin root.") from exc
        return raw_path

    def _write_journal(self, data: Mapping[str, Any]) -> None:
        _atomic_json_write(self.journal_path, data)

    def _clear_journal(self) -> None:
        try:
            self.journal_path.unlink()
        except FileNotFoundError:
            pass

    def _recover_transaction(self) -> None:
        """Recover a lifecycle interrupted between filesystem and registry commits."""
        if not self.journal_path.exists():
            return
        try:
            journal = _strict_json_loads(self.journal_path.read_text(encoding="utf-8"))
            if not isinstance(journal, dict):
                raise ValueError("journal is not an object")
            plugin_id = _safe_id(str(journal.get("plugin_id", "")))
            operation = str(journal.get("operation", ""))
            phase = str(journal.get("phase", ""))
            target = self._managed_plugin_path(plugin_id)
            backup = self._managed_staging_path(str(journal.get("backup", ""))) if journal.get("backup") else None
            candidate = self._managed_staging_path(str(journal.get("candidate", ""))) if journal.get("candidate") else None
            records = self._read_registry()
        except Exception as exc:
            raise PluginManagerError(f"Plugin transaction journal is unsafe or unreadable: {exc}") from exc

        if operation not in {"install", "remove"} or phase not in {
            "prepared",
            "backup-pending",
            "backup-moved",
            "promotion-pending",
            "promoted",
            "registry-pending",
            "registry-published",
        }:
            raise PluginManagerError("Plugin transaction journal contains an unknown operation or phase.")

        if operation == "install":
            published = (
                isinstance(records.get(plugin_id), dict)
                and records[plugin_id].get("archive_sha256") == journal.get("new_archive_sha256")
            )
            if published:
                if backup and backup.exists():
                    shutil.rmtree(backup, ignore_errors=True)
                if candidate and candidate.exists():
                    shutil.rmtree(candidate, ignore_errors=True)
            elif phase == "prepared":
                if candidate and candidate.exists():
                    shutil.rmtree(candidate, ignore_errors=True)
            else:
                if phase == "backup-pending" and target.exists() and not backup:
                    if candidate and candidate.exists():
                        shutil.rmtree(candidate, ignore_errors=True)
                    self._clear_journal()
                    return
                if target.exists():
                    if _is_link_or_reparse(target):
                        raise PluginManagerError("Interrupted transaction target is a symlink or reparse point.")
                    shutil.rmtree(target)
                if backup and backup.exists():
                    os.replace(str(backup), str(target))
                if candidate and candidate.exists():
                    shutil.rmtree(candidate, ignore_errors=True)
        else:
            if not self.registry_path.exists():
                raise PluginManagerError("Cannot recover removal without the authoritative plugin registry.")
            published = plugin_id not in records
            if published:
                if target.exists() and _is_link_or_reparse(target):
                    raise PluginManagerError("Interrupted removal target is a symlink or reparse point.")
                if backup and backup.exists():
                    shutil.rmtree(backup, ignore_errors=True)
            elif phase != "prepared":
                if target.exists() and _is_link_or_reparse(target):
                    raise PluginManagerError("Interrupted removal target is a symlink or reparse point.")
                if target.exists():
                    shutil.rmtree(target)
                if backup and backup.exists():
                    os.replace(str(backup), str(target))
        self._clear_journal()

    @staticmethod
    def _fetch_json(url: str) -> Mapping[str, Any]:
        try:
            with httpx.Client(timeout=30.0, follow_redirects=False) as client:
                with client.stream(
                    "GET",
                    url,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "Aetherion-CLI-plugin-manager/1.0",
                    },
                ) as response:
                    if response.status_code in (403, 429):
                        reset = response.headers.get("x-ratelimit-reset", "unknown")
                        raise PluginManagerError(
                            f"GitHub API rate limit or access denial (HTTP {response.status_code}; reset {reset})."
                        )
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    if content_type and "json" not in content_type:
                        raise PluginManagerError("GitHub metadata response had an unexpected content type.")
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > MAX_METADATA_BYTES:
                        raise PluginManagerError("GitHub metadata response exceeded the 512 KiB limit.")
                    chunks: List[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        total += len(chunk)
                        if total > MAX_METADATA_BYTES:
                            raise PluginManagerError("GitHub metadata response exceeded the 512 KiB limit.")
                        chunks.append(chunk)
                data = _strict_json_loads(b"".join(chunks).decode("utf-8"))
        except Exception as exc:
            if isinstance(exc, PluginManagerError):
                raise
            raise PluginManagerError(f"GitHub metadata fetch failed: {exc}") from exc
        if not isinstance(data, Mapping):
            raise PluginManagerError("GitHub metadata response was not an object.")
        return data

    @staticmethod
    def _fetch_bytes(url: str) -> bytes:
        def download(client: httpx.Client, target: str) -> Tuple[bytes, httpx.Response]:
            with client.stream(
                "GET",
                target,
                headers={"Accept": "application/octet-stream", "User-Agent": "Aetherion-CLI-plugin-manager/1.0"},
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    return b"", response
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if content_type and not any(
                    marker in content_type for marker in ("zip", "octet-stream", "binary")
                ):
                    raise PluginManagerError("GitHub archive response had an unexpected content type.")
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > MAX_ARCHIVE_BYTES:
                    raise PluginManagerError("GitHub archive exceeds the 64 MiB download limit.")
                chunks: List[bytes] = []
                total = 0
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise PluginManagerError("GitHub archive exceeds the 64 MiB download limit.")
                    chunks.append(chunk)
                return b"".join(chunks), response

        try:
            with httpx.Client(timeout=60.0, follow_redirects=False) as client:
                data, response = download(client, url)
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location", "")
                    parsed = urlsplit(location)
                    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "codeload.github.com":
                        raise PluginManagerError("GitHub archive redirect left the trusted codeload host.")
                    data, _response = download(client, location)
        except PluginManagerError:
            raise
        except Exception as exc:
            raise PluginManagerError(f"GitHub archive fetch failed: {exc}") from exc
        if len(data) > MAX_ARCHIVE_BYTES:
            raise PluginManagerError("GitHub archive exceeds the 64 MiB download limit.")
        return data

    @staticmethod
    def _resolve_commit(source_url: str, requested_ref: str, metadata_fetcher: Callable[[str], Mapping[str, Any]]) -> str:
        if _SHA_RE.fullmatch(requested_ref):
            return requested_ref.lower()
        parts = urlsplit(source_url).path.strip("/").split("/")
        owner, repository = parts
        url = (
            f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
            f"/commits/{quote(requested_ref, safe='')}"
        )
        data = metadata_fetcher(url)
        sha = data.get("sha")
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            raise PluginManagerError("GitHub did not return a valid immutable commit SHA.")
        return sha.lower()

    @staticmethod
    def _archive_url(source_url: str, commit: str) -> str:
        parts = urlsplit(source_url).path.strip("/").split("/")
        owner, repository = parts
        return f"https://codeload.github.com/{quote(owner)}/{quote(repository)}/zip/{commit}"

    def _resolve_selector(
        self,
        selector: str,
    ) -> Tuple[str, Optional[CatalogEntry], Optional[Dict[str, Any]]]:
        for entry in CURATED_CATALOG:
            if selector == entry.id:
                return entry.source_url, entry, None
        user_catalog = self._read_catalog()
        if selector in user_catalog:
            return str(user_catalog[selector]["source_url"]), None, user_catalog[selector]
        source_url = canonicalize_github_url(selector)
        user_entry = next(
            (record for record in user_catalog.values() if record.get("source_url") == source_url),
            None,
        )
        return source_url, None, user_entry

    def _resolve_requested_ref(
        self,
        source_url: str,
        catalog_entry: Optional[CatalogEntry],
        user_catalog_entry: Optional[Mapping[str, Any]],
        ref: Optional[str],
        allow_user_catalog_ref: bool = False,
    ) -> Tuple[str, str, bool]:
        """Return requested ref, immutable commit, and catalog verification state."""
        requested_ref = validate_ref(ref)
        effective_ref = requested_ref
        catalog_verified = False
        if catalog_entry:
            if ref is None or requested_ref == DEFAULT_REF:
                # A curated short ID is reproducibly pinned to the catalog
                # commit; ``main`` is retained only as user-facing provenance.
                effective_ref = catalog_entry.verified_commit
            elif requested_ref != catalog_entry.verified_commit:
                raise PluginManagerError(
                    "Curated sources accept only their verified commit; use the full repository URL for another ref."
                )
            catalog_verified = True
        elif user_catalog_entry and ref is None:
            requested_ref = validate_ref(str(user_catalog_entry.get("requested_ref", DEFAULT_REF)))
            pinned_commit = str(user_catalog_entry.get("resolved_commit", ""))
            if not _SHA_RE.fullmatch(pinned_commit):
                raise PluginManagerError("Saved catalog entry has no valid pinned commit.")
            effective_ref = pinned_commit
        elif user_catalog_entry:
            pinned_commit = str(user_catalog_entry.get("resolved_commit", ""))
            if not _SHA_RE.fullmatch(pinned_commit):
                raise PluginManagerError("Saved catalog entry has no valid pinned commit.")
            if not allow_user_catalog_ref and requested_ref.lower() != pinned_commit.lower():
                raise PluginManagerError(
                    "Saved catalog aliases install only their pinned commit; use 'plugins catalog pin' first."
                )
            effective_ref = pinned_commit if not allow_user_catalog_ref else requested_ref
        resolved_commit = self._resolve_commit(source_url, effective_ref, self._json_fetcher)
        if catalog_entry and resolved_commit != catalog_entry.verified_commit:
            raise PluginManagerError("Curated source resolved to a commit different from its verified catalog commit.")
        return requested_ref, resolved_commit, catalog_verified

    @staticmethod
    def _catalog_subject(entry: CatalogEntry, record: Mapping[str, Any]) -> CatalogSubject:
        return CatalogSubject(
            plugin_id=entry.id,
            repository_url=str(record.get("source_url", entry.source_url)),
            commit_sha=str(record.get("resolved_commit", "")),
            archive_sha256=str(record.get("archive_sha256", "")),
            archive_size_bytes=int(record.get("archive_size", 0) or 0),
            manifest_path=str(record.get("manifest_file") or ""),
            manifest_sha256=str(record.get("manifest_sha256", "")),
            manifest_schema_version=str(record.get("manifest_schema_version", "")),
            plugin_api_version=str(record.get("plugin_api_version", "")),
            compatibility=str(record.get("compatibility_status", "unknown")),
            execution_enabled=False,
        )

    def _catalog_fields(
        self,
        record: Mapping[str, Any],
        snapshot: Any,
        verification: Any,
    ) -> Dict[str, Any]:
        fields = trust_fields_for_record(record, verification)
        fields.update(
            {
                "catalog_freshness": getattr(snapshot, "freshness", "unavailable"),
                "catalog_source": getattr(snapshot, "source", "none"),
                "signed_catalog_id": getattr(snapshot, "catalog_id", None),
                "catalog_issued_at": getattr(snapshot, "issued_at", None),
                "catalog_expires_at": getattr(snapshot, "expires_at", None),
                "catalog_endpoint": getattr(snapshot, "endpoint", None),
                "catalog_trust_root_fingerprint": getattr(snapshot, "trust_root_fingerprint", None),
                "catalog_refresh_failure_reason": getattr(snapshot, "refresh_failure_reason", None),
            }
        )
        fields.update(
            {
                "catalog_keyset_id": getattr(snapshot, "trust_keyset_id", None),
                "catalog_keyset_sequence": getattr(snapshot, "trust_keyset_sequence", None),
                "catalog_keyset_expires_at": getattr(snapshot, "trust_keyset_expires_at", None),
                "catalog_keyset_active_key_ids": list(getattr(snapshot, "trust_keyset_active_key_ids", ())),
                "catalog_keyset_revoked_key_ids": list(getattr(snapshot, "trust_keyset_revoked_key_ids", ())),
                "catalog_trust_lifecycle_reason": getattr(snapshot, "trust_lifecycle_reason", None),
                "catalog_trust_last_transition": getattr(snapshot, "trust_last_transition", None),
            }
        )
        return fields

    @staticmethod
    def _publisher_subject(record: Mapping[str, Any]) -> Optional[PublisherEvidenceSubject]:
        source_url = record.get("source_url") or record.get("repository")
        commit = record.get("resolved_commit") or record.get("verified_commit")
        if not isinstance(source_url, str) or not isinstance(commit, str) or not _SHA_RE.fullmatch(commit):
            return None
        try:
            return PublisherEvidenceSubject.create(source_url, commit)
        except PublisherEvidenceError:
            return None

    def _publisher_evidence_fields(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        subject = self._publisher_subject(record)
        if subject is None:
            return {}
        try:
            evidence = self.publisher_evidence_store.get(subject)
        except PublisherEvidenceError as exc:
            raise PluginManagerError(str(exc)) from exc
        if evidence is None:
            return {}
        return {
            "publisher_auth_status": evidence.publisher_auth_status.value,
            "publisher_evidence": evidence.to_dict(),
            "publisher_evidence_provider": evidence.provider,
            "publisher_evidence_type": evidence.evidence_type,
            "publisher_evidence_observation_status": evidence.observation_status.value,
            "publisher_evidence_provider_status": evidence.provider_status.value if evidence.provider_status else None,
            "publisher_evidence_reason": evidence.reason_code,
            "publisher_evidence_provider_reason": evidence.provider_reason,
            "publisher_evidence_identity_policy_status": evidence.identity_policy_status.value,
            "publisher_evidence_local_signature_status": evidence.local_signature_status.value,
            "publisher_evidence_signature_format": evidence.signature_format,
            "publisher_evidence_observed_at": evidence.observed_at,
            "publisher_evidence_freshness": evidence.freshness,
            "publisher_evidence_commit": evidence.subject.commit_sha,
            "publisher_evidence_digest": evidence.evidence_digest,
        }

    def _verify_curated_record(
        self,
        entry: CatalogEntry,
        record: Mapping[str, Any],
        *,
        refresh: bool,
    ) -> tuple[Dict[str, Any], Any]:
        snapshot = self.catalog_service.snapshot(refresh=refresh)
        verification = (
            self.catalog_service.verify_subject(snapshot, self._catalog_subject(entry, record))
            if snapshot.raw is not None
            else snapshot.result
        )
        return self._catalog_fields(record, snapshot, verification), verification

    def _require_curated_approval(self, entry: CatalogEntry, record: Mapping[str, Any]) -> Dict[str, Any]:
        fields, verification = self._verify_curated_record(entry, record, refresh=True)
        if not verification.verified or fields.get("policy_status") != "catalog_approved":
            reason = verification.reason_code.value if verification.reason_code else "catalog_approval_unavailable"
            raise PluginManagerError(
                f"Curated source '{entry.id}' requires a valid signed catalog statement; {reason}."
            )
        if fields.get("execution_enabled") is not False or fields.get("execution_status") != "disabled":
            raise PluginManagerError("Signed catalog policy cannot enable plugin execution in Option A.")
        return fields

    def _curated_display_fields(self, entry: CatalogEntry) -> Dict[str, Any]:
        if self.catalog_service is None:
            fields = trust_fields_for_record(entry.to_dict())
            fields.update(
                {
                    "catalog_freshness": "unavailable",
                    "catalog_source": "none",
                    "signed_catalog_id": None,
                    "catalog_issued_at": None,
                    "catalog_expires_at": None,
                    "catalog_endpoint": None,
                    "catalog_trust_root_fingerprint": None,
                    "catalog_refresh_failure_reason": None,
                    "catalog_keyset_id": None,
                    "catalog_keyset_sequence": None,
                    "catalog_keyset_expires_at": None,
                    "catalog_keyset_active_key_ids": [],
                    "catalog_keyset_revoked_key_ids": [],
                    "catalog_trust_lifecycle_reason": None,
                    "catalog_trust_last_transition": None,
                }
            )
            return fields
        snapshot = self.catalog_service.snapshot(refresh=False)
        verification = snapshot.result
        fields = self._catalog_fields(entry.to_dict(), snapshot, verification)
        fields.update(self._publisher_evidence_fields(entry.to_dict()))
        return fields

    def _read_registry(self) -> Dict[str, Dict[str, Any]]:
        if _is_link_or_reparse(self.registry_path):
            raise PluginManagerError("Plugin registry cannot be a symlink or reparse point.")
        if not self.registry_path.exists():
            return {}
        try:
            data = _strict_json_loads(self.registry_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise PluginManagerError(f"Plugin registry is unreadable: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != 1:
            raise PluginManagerError("Plugin registry has an unsupported schema version.")
        plugins = data.get("plugins")
        if not isinstance(plugins, dict):
            raise PluginManagerError("Plugin registry has an invalid plugins section.")
        if any(not isinstance(key, str) or not isinstance(value, dict) for key, value in plugins.items()):
            raise PluginManagerError("Plugin registry contains a malformed plugin record.")
        return {key: dict(value) for key, value in plugins.items()}

    def _write_registry(self, records: Mapping[str, Mapping[str, Any]]) -> None:
        if _is_link_or_reparse(self.registry_path):
            raise PluginManagerError("Plugin registry cannot be a symlink or reparse point.")
        _atomic_json_write(self.registry_path, {"version": 1, "plugins": dict(records)})

    @staticmethod
    def _catalog_id(value: str) -> str:
        alias = _safe_id(value, "catalog id")
        reserved = {entry.id.casefold() for entry in CURATED_CATALOG}
        if alias.casefold() in reserved:
            raise PluginManagerError(f"Catalog ID '{alias}' is reserved for a curated source.")
        return alias

    def _read_catalog(self) -> Dict[str, Dict[str, Any]]:
        if _is_link_or_reparse(self.catalog_path):
            raise PluginManagerError("Plugin catalog cannot be a symlink or reparse point.")
        if not self.catalog_path.exists():
            return {}
        try:
            if self.catalog_path.stat().st_size > MAX_METADATA_BYTES:
                raise PluginManagerError("Plugin catalog exceeds the 512 KiB limit.")
            data = _strict_json_loads(self.catalog_path.read_text(encoding="utf-8"))
        except PluginManagerError:
            raise
        except Exception as exc:
            raise PluginManagerError(f"Plugin catalog is unreadable: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != CATALOG_VERSION:
            raise PluginManagerError("Plugin catalog has an unsupported schema version.")
        entries = data.get("entries")
        if not isinstance(entries, dict):
            raise PluginManagerError("Plugin catalog has an invalid entries section.")
        normalized: Dict[str, Dict[str, Any]] = {}
        for key, value in entries.items():
            alias = self._catalog_id(str(key))
            if not isinstance(value, dict) or value.get("id") != alias:
                raise PluginManagerError(f"Plugin catalog entry '{alias}' is malformed.")
            source_url = canonicalize_github_url(str(value.get("source_url", "")))
            requested_ref = validate_ref(str(value.get("requested_ref", DEFAULT_REF)))
            resolved_commit = str(value.get("resolved_commit", ""))
            if not _SHA_RE.fullmatch(resolved_commit):
                raise PluginManagerError(f"Plugin catalog entry '{alias}' has no valid pinned commit.")
            source_plugin_id = str(value.get("source_plugin_id", ""))
            if not _ID_RE.fullmatch(source_plugin_id):
                raise PluginManagerError(f"Plugin catalog entry '{alias}' has no valid source plugin ID.")
            record = dict(value)
            record["source_url"] = source_url
            record["requested_ref"] = requested_ref
            record["resolved_commit"] = resolved_commit.lower()
            record["source_plugin_id"] = source_plugin_id
            record["source_type"] = "user"
            record["catalog_origin"] = "user"
            record["execution_enabled"] = False
            record.update(
                trust_fields_for_record(
                    {
                        **record,
                        "integrity_status": "unknown",
                        "catalog_auth_status": "unsigned",
                        "publisher_auth_status": "not_checked",
                        "policy_status": "unreviewed",
                        "catalog_key_id": None,
                    }
                )
            )
            for forged_field in (
                "catalog_sequence",
                "signed_catalog_id",
                "catalog_issued_at",
                "catalog_expires_at",
                "catalog_endpoint",
                "catalog_trust_root_fingerprint",
                "catalog_keyset_id",
                "catalog_keyset_sequence",
                "catalog_keyset_expires_at",
                "catalog_keyset_active_key_ids",
                "catalog_keyset_revoked_key_ids",
                "catalog_trust_lifecycle_reason",
                "catalog_trust_last_transition",
                "catalog_freshness",
                "catalog_source",
                "publisher_auth_status",
                "publisher_evidence",
                "publisher_evidence_provider",
                "publisher_evidence_type",
                "publisher_evidence_observation_status",
                "publisher_evidence_provider_status",
                "publisher_evidence_reason",
                "publisher_evidence_provider_reason",
                "publisher_evidence_identity_policy_status",
                "publisher_evidence_local_signature_status",
                "publisher_evidence_signature_format",
                "publisher_evidence_observed_at",
                "publisher_evidence_freshness",
                "publisher_evidence_commit",
                "publisher_evidence_digest",
            ):
                record.pop(forged_field, None)
            normalized[alias] = record
        urls = [record["source_url"] for record in normalized.values()]
        if len(urls) != len(set(urls)):
            raise PluginManagerError("Plugin catalog contains duplicate repository URLs.")
        return normalized

    def _write_catalog(self, records: Mapping[str, Mapping[str, Any]]) -> None:
        if _is_link_or_reparse(self.catalog_path):
            raise PluginManagerError("Plugin catalog cannot be a symlink or reparse point.")
        _atomic_json_write(self.catalog_path, {"version": CATALOG_VERSION, "entries": dict(records)})

    @staticmethod
    def _github_api_repo_url(source_url: str) -> str:
        parts = urlsplit(source_url).path.strip("/").split("/")
        owner, repository = parts
        return f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repository, safe='')}"

    def _fetch_repository_metadata(self, source_url: str) -> Dict[str, Any]:
        data = self._json_fetcher(self._github_api_repo_url(source_url))
        if not isinstance(data, Mapping):
            raise PluginManagerError("GitHub repository metadata was not an object.")
        returned_url = data.get("html_url") or data.get("clone_url")
        if returned_url:
            try:
                if canonicalize_github_url(str(returned_url)) != source_url:
                    raise PluginManagerError("GitHub metadata URL did not match the requested repository.")
            except PluginManagerError:
                raise
        license_value = data.get("license")
        license_id = ""
        license_name = ""
        if isinstance(license_value, Mapping):
            license_id = _clean_text(license_value.get("spdx_id"), "license.spdx_id", 128)
            license_name = _clean_text(license_value.get("name"), "license.name", 256)
        topics = data.get("topics") or []
        if not isinstance(topics, list):
            topics = []
        safe_topics = [_clean_text(topic, "topic", 128) for topic in topics if isinstance(topic, str)][:50]
        language = _clean_text(data.get("language"), "language", 128)
        description = _clean_text(data.get("description"), "description", 4096)
        name = _clean_text(data.get("name"), "name", 256)
        full_name = _clean_text(data.get("full_name"), "full_name", 256)
        default_branch = _clean_text(data.get("default_branch") or DEFAULT_REF, "default_branch", 256)
        return {
            "name": name,
            "full_name": full_name,
            "description": description,
            "source_url": source_url,
            "language": language,
            "topics": safe_topics,
            "archived": bool(data.get("archived", False)),
            "stars": _safe_count(data.get("stargazers_count"), "stargazers_count"),
            "forks": _safe_count(data.get("forks_count"), "forks_count"),
            "license_id": license_id,
            "license_name": license_name,
            "license_declared": bool(license_id or license_name),
            "pushed_at": _clean_text(data.get("pushed_at"), "pushed_at", 128),
            "updated_at": _clean_text(data.get("updated_at"), "updated_at", 128),
            "default_branch": default_branch,
            "homepage": _clean_text(data.get("homepage"), "homepage", 1024),
        }

    @staticmethod
    def _validate_archive_member(info: zipfile.ZipInfo, seen: set[str]) -> str:
        name = unicodedata.normalize("NFC", info.filename)
        if not name or _CONTROL_RE.search(name) or "\\" in name:
            raise PluginManagerError("Archive contains an invalid path.")
        if name.startswith("/") or ":" in name:
            raise PluginManagerError(f"Archive contains an absolute or stream path: {name}")
        raw_parts = name.split("/")
        if info.is_dir() and raw_parts and raw_parts[-1] == "":
            raw_parts = raw_parts[:-1]
        if not raw_parts or any(part in ("", ".", "..") for part in raw_parts):
            raise PluginManagerError(f"Archive contains a normalized or traversal path: {name}")
        if os.name == "nt" and any(part.rstrip(" .") != part for part in raw_parts):
            raise PluginManagerError(f"Archive contains a Windows-ambiguous path: {name}")
        parts = PurePosixPath(name).parts
        canonical = "/".join(parts)
        collision_key = canonical.casefold() if os.name == "nt" else canonical
        if collision_key in seen:
            raise PluginManagerError(f"Archive contains a duplicate or colliding path: {name}")
        seen.add(collision_key)
        if len(name) > 512:
            raise PluginManagerError("Archive contains an overlong path.")
        unix_mode = info.external_attr >> 16
        if (unix_mode & 0o170000) == 0o120000:
            raise PluginManagerError(f"Archive symlinks are not allowed: {name}")
        base = PurePosixPath(name).name.lower()
        if base in _PROTECTED_NAMES or PurePosixPath(base).suffix.lower() in _PROTECTED_SUFFIXES:
            raise PluginManagerError(f"Archive contains a protected credential/key file: {name}")
        return "/".join(parts)

    def _safe_extract(self, archive: bytes, destination: Path) -> Path:
        if len(archive) > MAX_ARCHIVE_BYTES:
            raise PluginManagerError("Archive exceeds the 64 MiB download limit.")
        if destination.exists() and _is_link_or_reparse(destination):
            raise PluginManagerError("Extraction destination cannot be a symlink or reparse point.")
        destination = destination.resolve(strict=False)
        try:
            destination.relative_to(self.staging_dir.resolve())
        except ValueError as exc:
            raise PluginManagerError("Extraction destination escaped managed staging.") from exc
        destination.mkdir(parents=True, exist_ok=True)
        if _is_link_or_reparse(destination):
            raise PluginManagerError("Extraction destination cannot be a symlink or reparse point.")
        seen: set[str] = set()
        total = 0
        try:
            with zipfile.ZipFile(BytesIO(archive), "r") as zf:
                infos = zf.infolist()
                if len(infos) > MAX_ARCHIVE_FILES:
                    raise PluginManagerError("Archive exceeds the 5,000-entry limit.")
                for info in infos:
                    name = self._validate_archive_member(info, seen)
                    if info.is_dir():
                        continue
                    if info.flag_bits & 0x1:
                        raise PluginManagerError("Encrypted plugin archives are not supported.")
                    if info.file_size > MAX_FILE_BYTES:
                        raise PluginManagerError(f"Archive file exceeds the 10 MiB file limit: {name}")
                    total += info.file_size
                    if total > MAX_ARCHIVE_BYTES:
                        raise PluginManagerError("Archive uncompressed content exceeds the 64 MiB limit.")
                    target = (destination / name).resolve()
                    try:
                        target.relative_to(destination)
                    except ValueError as exc:
                        raise PluginManagerError("Archive path escaped its extraction directory.") from exc
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info, "r") as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
        except zipfile.BadZipFile as exc:
            raise PluginManagerError("Downloaded source is not a valid ZIP archive.") from exc

        children = list(destination.iterdir())
        directories = [child for child in children if child.is_dir()]
        files = [child for child in children if child.is_file()]
        if len(directories) == 1 and not files:
            return directories[0]
        return destination

    @staticmethod
    def _manifest_candidate(root: Path) -> Path:
        for name in ("aetherion.plugin.json", "plugin.json", "manifest.json", "package.json"):
            candidate = root / name
            if candidate.is_file():
                return candidate
        raise PluginManagerError(
            "No supported plugin metadata found. Expected aetherion.plugin.json, plugin.json, manifest.json, or package.json."
        )

    @staticmethod
    def _repository_value(value: Any) -> str:
        if isinstance(value, Mapping):
            value = value.get("url") or value.get("directory") or ""
        if not isinstance(value, str):
            return ""
        value = value.strip()
        if value.startswith("git+https://"):
            value = value[4:]
        return value

    def _parse_manifest(
        self,
        root: Path,
        source_url: str,
        catalog_entry: Optional[CatalogEntry],
    ) -> Dict[str, Any]:
        manifest_path = self._manifest_candidate(root)
        raw_bytes = manifest_path.read_bytes()
        if len(raw_bytes) > MAX_MANIFEST_BYTES:
            raise PluginManagerError("Plugin metadata exceeds the 128 KiB limit.")
        try:
            raw = _strict_json_loads(raw_bytes.decode("utf-8"))
        except Exception as exc:
            raise PluginManagerError(f"Plugin metadata is not valid UTF-8 JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise PluginManagerError("Plugin metadata must be a JSON object.")

        def text_value(key: str, field: Optional[str] = None, required: bool = False, maximum: int = MAX_TEXT_BYTES) -> str:
            if key not in raw:
                if required:
                    raise PluginManagerError(f"Plugin metadata must declare '{key}'.")
                return ""
            value = raw[key]
            if not isinstance(value, str):
                raise PluginManagerError(f"Plugin metadata field '{field or key}' must be a string.")
            return _clean_text(value, field or key, maximum)

        marker = raw.get("aetherion_plugin")
        if marker is not None and not isinstance(marker, bool):
            raise PluginManagerError("Plugin metadata field 'aetherion_plugin' must be boolean.")
        explicit_aetherion_plugin = manifest_path.name == "aetherion.plugin.json" or marker is True
        if manifest_path.name == "aetherion.plugin.json" and marker is False:
            raise PluginManagerError("Aetherion plugin manifests cannot disable their plugin marker.")

        schema_keys = [key for key in ("manifest_schema_version", "schema_version") if key in raw]
        if len(schema_keys) == 2 and raw[schema_keys[0]] != raw[schema_keys[1]]:
            raise PluginManagerError("Plugin metadata declares conflicting schema versions.")
        schema_key = schema_keys[0] if schema_keys else "manifest_schema_version"
        schema_version = text_value(schema_key, required=explicit_aetherion_plugin)
        if explicit_aetherion_plugin:
            if not _MANIFEST_SCHEMA_VERSION_RE.fullmatch(schema_version):
                raise PluginManagerError(
                    f"Unsupported Aetherion manifest schema version: {schema_version!r}."
                )
        elif schema_version and not _MANIFEST_SCHEMA_VERSION_RE.fullmatch(schema_version):
            raise PluginManagerError(f"Invalid plugin metadata schema version: {schema_version!r}.")

        api_keys = [key for key in ("plugin_api_version", "aetherion_plugin_api") if key in raw]
        if len(api_keys) == 2 and raw[api_keys[0]] != raw[api_keys[1]]:
            raise PluginManagerError("Plugin metadata declares conflicting plugin API versions.")
        api_key = api_keys[0] if api_keys else "plugin_api_version"
        plugin_api_version = text_value(api_key, "plugin_api_version", required=explicit_aetherion_plugin, maximum=128)
        if plugin_api_version and not _PLUGIN_API_VERSION_RE.fullmatch(plugin_api_version):
            raise PluginManagerError(f"Invalid plugin API version: {plugin_api_version!r}.")

        package_fallback = manifest_path.name == "package.json"
        if "repository" in raw and not isinstance(raw["repository"], (str, Mapping)):
            raise PluginManagerError("Plugin metadata field 'repository' must be a string or object.")
        declared_repository = self._repository_value(raw.get("repository"))
        if declared_repository:
            try:
                canonical_declared = canonicalize_github_url(declared_repository.rstrip("/"))
            except PluginManagerError as exc:
                raise PluginManagerError("Plugin metadata declares a non-GitHub repository.") from exc
            if canonical_declared != source_url:
                raise PluginManagerError("Plugin metadata repository does not match the fetched GitHub repository.")

        name = text_value("name", maximum=256) or (catalog_entry.name if catalog_entry else "")
        if len(name) < 2:
            raise PluginManagerError("Plugin metadata must declare a name of at least two characters.")
        version = text_value("version", maximum=128) or (catalog_entry.version if catalog_entry else "")
        if not _VERSION_RE.fullmatch(version):
            raise PluginManagerError(f"Invalid plugin version: {version!r}")

        declared_id = text_value("id", maximum=128) if "id" in raw else ""
        if explicit_aetherion_plugin and not declared_id:
            raise PluginManagerError("Aetherion plugin manifests must declare an id.")
        if declared_id:
            plugin_id = _safe_id(declared_id)
        elif catalog_entry:
            plugin_id = catalog_entry.id
        else:
            plugin_id = _derived_id(name)
        if catalog_entry and declared_id and plugin_id != catalog_entry.id:
            raise PluginManagerError("Plugin metadata ID does not match its curated catalog ID.")

        description = text_value("description", maximum=4096)
        author_value = raw.get("author")
        if author_value is not None and not isinstance(author_value, (str, Mapping)):
            raise PluginManagerError("Plugin metadata field 'author' must be a string or object.")
        if isinstance(author_value, Mapping):
            author_value = author_value.get("name") or author_value.get("email") or ""
        if author_value and not isinstance(author_value, str):
            raise PluginManagerError("Plugin metadata author values must be strings.")
        author = _clean_text(author_value or (catalog_entry.author if catalog_entry else "community"), "author", 256)
        license_value = raw.get("license")
        if license_value is not None and not isinstance(license_value, (str, Mapping)):
            raise PluginManagerError("Plugin metadata field 'license' must be a string or object.")
        if isinstance(license_value, Mapping):
            license_value = license_value.get("type") or license_value.get("name") or ""
        if license_value and not isinstance(license_value, str):
            raise PluginManagerError("Plugin metadata license values must be strings.")
        license_name = _clean_text(license_value or (catalog_entry.license if catalog_entry else ""), "license", 128)
        if catalog_entry and license_name and license_name.upper() != catalog_entry.license.upper():
            raise PluginManagerError("Curated plugin license metadata does not match the verified catalog license.")

        for entrypoint_key in ("entrypoint", "main"):
            if entrypoint_key in raw and raw[entrypoint_key] is not None and not isinstance(raw[entrypoint_key], str):
                raise PluginManagerError(f"Plugin metadata field '{entrypoint_key}' must be a string.")
        entrypoint = text_value("entrypoint", maximum=512) or text_value("main", maximum=512)
        if entrypoint:
            entry_path = entrypoint.split(":", 1)[0]
            if entry_path.startswith(("/", "\\")) or ".." in PurePosixPath(entry_path).parts or ":" in entry_path:
                raise PluginManagerError("Plugin entrypoint must be a relative informational path.")

        dependencies: List[str] = []
        declared_dependencies = raw.get("required_dependencies") or raw.get("dependencies") or []
        if isinstance(declared_dependencies, Mapping):
            dependencies.extend(_clean_text(key, "dependency", 512) for key in declared_dependencies.keys())
        elif isinstance(declared_dependencies, list):
            if any(not isinstance(value, str) for value in declared_dependencies):
                raise PluginManagerError("Plugin dependencies must contain only strings.")
            dependencies.extend(_clean_text(value, "dependency", 512) for value in declared_dependencies)
        elif declared_dependencies:
            raise PluginManagerError("Plugin dependencies must be a list or object.")
        optional_dependencies = raw.get("optionalDependencies") or {}
        if isinstance(optional_dependencies, Mapping):
            dependencies.extend(str(key) for key in optional_dependencies.keys())
        dependencies = sorted(set(dependency for dependency in dependencies if dependency))

        permissions = raw.get("required_permissions") or raw.get("permissions") or []
        if isinstance(permissions, str):
            permissions = [permissions]
        if not isinstance(permissions, list) or any(not isinstance(item, str) for item in permissions):
            raise PluginManagerError("Plugin permissions must be a list of strings.")
        permissions = sorted({_clean_text(item, "permission", 128) for item in permissions if item})

        tags = raw.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        if not isinstance(tags, list) or any(not isinstance(item, str) for item in tags):
            raise PluginManagerError("Plugin tags must be a list of strings.")
        tags = [_clean_text(item, "tag", 128) for item in tags if item]

        runtime = text_value("runtime", maximum=128) or (
            "node" if package_fallback else "unknown"
        )
        compatible_aetherion_version = text_value("compatible_aetherion_version", maximum=128)
        manifest_type = text_value("type", maximum=128)
        homepage = text_value("homepage", maximum=1024)
        documentation = text_value("documentation", maximum=1024)
        if catalog_entry:
            homepage = homepage or catalog_entry.homepage
            documentation = documentation or catalog_entry.documentation
        if "scripts" in raw and raw["scripts"] is not None and not isinstance(raw["scripts"], (Mapping, list)):
            raise PluginManagerError("Plugin scripts metadata must be an object or list.")
        provided_tools = raw.get("provided_tools", [])
        if not isinstance(provided_tools, list):
            raise PluginManagerError("Plugin provided_tools metadata must be a list.")

        return {
            "id": plugin_id,
            "name": name,
            "version": version,
            "description": description,
            "author": author,
            "license": license_name,
            "entrypoint": entrypoint,
            "runtime": runtime,
            "repository": source_url,
            "homepage": homepage,
            "documentation": documentation,
            "dependencies": dependencies,
            "requested_permissions": permissions,
            "tags": tags,
            "manifest_file": manifest_path.name,
            "package_metadata": package_fallback,
            "source_only": False,
            "plugin_api_version": plugin_api_version,
            "compatible_aetherion_version": compatible_aetherion_version,
            "manifest_type": manifest_type,
            "manifest_schema_version": schema_version,
            "explicit_aetherion_plugin": explicit_aetherion_plugin,
            "scripts_declared": bool(raw.get("scripts")),
            "provided_tools_count": len(provided_tools),
            "manifest_sha256": _sha256_bytes(raw_bytes),
        }

    @staticmethod
    def _source_only_manifest(
        source_url: str,
        repository_metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        parts = urlsplit(source_url).path.strip("/").split("/")
        repository_name = parts[-1] if parts else "repository"
        metadata = repository_metadata or {}
        name = _clean_text(metadata.get("name") or repository_name, "name", 256)
        if len(name) < 2:
            name = "source-repository"
        return {
            "id": _derived_id(repository_name),
            "name": name,
            "version": "0.0.0",
            "description": _clean_text(metadata.get("description"), "description", 4096),
            "author": _clean_text(parts[0] if parts else "community", "author", 256),
            "license": _clean_text(
                metadata.get("license_id") or metadata.get("license_name"),
                "license",
                128,
            ),
            "entrypoint": "",
            "runtime": _clean_text(metadata.get("language"), "runtime", 128) or "unknown",
            "repository": source_url,
            "homepage": _clean_text(metadata.get("homepage"), "homepage", 1024),
            "documentation": "",
            "dependencies": [],
            "requested_permissions": [],
            "tags": list(metadata.get("topics") or [])[:50],
            "manifest_file": None,
            "package_metadata": False,
            "source_only": True,
            "manifest_schema_version": "",
            "plugin_api_version": "",
            "compatible_aetherion_version": "",
            "manifest_type": "",
            "explicit_aetherion_plugin": False,
            "scripts_declared": False,
            "provided_tools_count": 0,
            "manifest_sha256": None,
        }

    @staticmethod
    def _classify_compatibility(manifest: Mapping[str, Any]) -> Dict[str, Any]:
        if manifest.get("source_only"):
            return {
                "status": "unknown",
                "reason_codes": ["no_supported_aetherion_manifest"],
                "classifier_version": 1,
            }
        if not manifest.get("explicit_aetherion_plugin"):
            return {
                "status": "unknown",
                "reason_codes": ["no_explicit_aetherion_plugin_contract"],
                "classifier_version": 1,
            }
        api_version = str(manifest.get("plugin_api_version") or "").strip()
        if api_version:
            major_text = api_version.split(".", 1)[0]
            if major_text.isdigit() and int(major_text) == SUPPORTED_PLUGIN_API_MAJOR:
                return {
                    "status": "compatible",
                    "reason_codes": ["explicit_supported_plugin_api"],
                    "classifier_version": 1,
                }
            return {
                "status": "incompatible",
                "reason_codes": ["unsupported_plugin_api"],
                "classifier_version": 1,
            }
        if manifest.get("explicit_aetherion_plugin"):
            return {
                "status": "unknown",
                "reason_codes": ["manifest_missing_plugin_api_version"],
                "classifier_version": 1,
            }
        return {
            "status": "unknown",
            "reason_codes": ["no_explicit_aetherion_plugin_contract"],
            "classifier_version": 1,
        }

    @staticmethod
    def _file_hashes(root: Path) -> Dict[str, str]:
        hashes: Dict[str, str] = {}
        total_bytes = 0
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            dir_path = Path(directory)
            for dirname in list(dirnames):
                if _is_link_or_reparse(dir_path / dirname):
                    raise PluginManagerError("Symlinks are not allowed in the installed plugin source.")
            for filename in sorted(filenames):
                path = dir_path / filename
                if _is_link_or_reparse(path):
                    raise PluginManagerError("Symlinks are not allowed in the installed plugin source.")
                relative = _relative_file(root, path)
                file_stat = os.lstat(path)
                if not stat.S_ISREG(file_stat.st_mode):
                    raise PluginManagerError(f"Plugin source contains a special file: {relative}")
                if file_stat.st_size > MAX_FILE_BYTES:
                    raise PluginManagerError(f"Plugin source file exceeds the 10 MiB limit: {relative}")
                total_bytes += file_stat.st_size
                if total_bytes > MAX_ARCHIVE_BYTES:
                    raise PluginManagerError("Plugin source exceeds the 64 MiB aggregate limit.")
                hashes[relative] = _sha256_file(path)
        if len(hashes) > MAX_ARCHIVE_FILES:
            raise PluginManagerError("Plugin source exceeds the 5,000-file limit.")
        return dict(sorted(hashes.items()))

    def _archive_metadata(
        self,
        archive: bytes,
        source_url: str,
        requested_ref: str,
        resolved_commit: str,
        catalog_entry: Optional[CatalogEntry],
        destination: Path,
        repository_metadata: Optional[Mapping[str, Any]] = None,
        allow_source_only: bool = True,
        user_catalog_entry: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], Path, Dict[str, str]]:
        root = self._safe_extract(archive, destination)
        try:
            manifest = self._parse_manifest(root, source_url, catalog_entry)
        except PluginManagerError as exc:
            if not allow_source_only or not str(exc).startswith("No supported plugin metadata found"):
                raise
            manifest = self._source_only_manifest(source_url, repository_metadata)
        file_hashes = self._file_hashes(root)
        compatibility = self._classify_compatibility(manifest)
        record = {
            "id": manifest["id"],
            "name": manifest["name"],
            "version": manifest["version"],
            "description": manifest["description"],
            "author": manifest["author"],
            "license": manifest["license"],
            "runtime": manifest["runtime"],
            "entrypoint": manifest["entrypoint"],
            "repository": source_url,
            "source_url": source_url,
            "requested_ref": requested_ref,
            "resolved_commit": resolved_commit,
            "archive_url": self._archive_url(source_url, resolved_commit),
            "archive_sha256": _sha256_bytes(archive),
            "archive_size": len(archive),
            "manifest_sha256": manifest["manifest_sha256"],
            "manifest_file": manifest["manifest_file"],
            "source_only": manifest["source_only"],
            "manifest_schema_version": manifest["manifest_schema_version"],
            "plugin_api_version": manifest["plugin_api_version"],
            "compatible_aetherion_version": manifest["compatible_aetherion_version"],
            "manifest_type": manifest["manifest_type"],
            "explicit_aetherion_plugin": manifest["explicit_aetherion_plugin"],
            "compatibility": compatibility,
            "compatibility_status": compatibility["status"],
            "compatibility_reasons": compatibility["reason_codes"],
            "dependencies": manifest["dependencies"],
            "requested_permissions": manifest["requested_permissions"],
            "tags": manifest["tags"],
            "homepage": manifest["homepage"],
            "documentation": manifest["documentation"],
            "package_metadata": manifest["package_metadata"],
            "scripts_declared": manifest["scripts_declared"],
            "provided_tools_count": manifest["provided_tools_count"],
            "execution_enabled": False,
            "status": "inspected",
            "source_root": _relative_file(destination, root),
            "file_count": len(file_hashes),
            "integrity": "not-installed",
        }
        record.update(trust_fields_for_record(record))
        if catalog_entry:
            record["catalog_id"] = catalog_entry.id
            record["curated"] = True
            record["curated_license"] = catalog_entry.license
            record["catalog_verified_commit"] = catalog_entry.verified_commit
            record["catalog_commit_verified"] = record["resolved_commit"] == catalog_entry.verified_commit
        else:
            record["curated"] = False
            record["source_type"] = "user" if user_catalog_entry else "custom"
            record["catalog_origin"] = "user" if user_catalog_entry else "none"
            if user_catalog_entry:
                record["catalog_id"] = user_catalog_entry.get("id")
        return record, root, file_hashes

    def inspect(
        self,
        selector: str,
        ref: Optional[str] = None,
        repository_metadata: Optional[Mapping[str, Any]] = None,
        allow_user_catalog_ref: bool = False,
    ) -> Dict[str, Any]:
        """Fetch and validate source metadata without storing or executing it."""
        source_url, catalog_entry, user_catalog_entry = self._resolve_selector(selector)
        requested_ref, resolved_commit, _catalog_verified = self._resolve_requested_ref(
            source_url,
            catalog_entry,
            user_catalog_entry,
            ref,
            allow_user_catalog_ref=allow_user_catalog_ref,
        )
        archive = self._bytes_fetcher(self._archive_url(source_url, resolved_commit))
        if not isinstance(archive, bytes):
            raise PluginManagerError("Archive fetcher returned a non-bytes value.")
        with tempfile.TemporaryDirectory(dir=str(self.staging_dir), prefix="inspect-") as temp_name:
            record, _root, _hashes = self._archive_metadata(
                archive,
                source_url,
                requested_ref,
                resolved_commit,
                catalog_entry,
                Path(temp_name),
                repository_metadata=repository_metadata,
                user_catalog_entry=user_catalog_entry,
            )
        if catalog_entry:
            trust_record, _verification = self._verify_curated_record(catalog_entry, record, refresh=True)
            record.update(trust_record)
        installed = self._read_registry().get(record["id"])
        result = dict(record)
        result.update(trust_fields_for_record(result))
        result.update(self._publisher_evidence_fields(result))
        result["installed"] = bool(installed)
        if installed:
            result["installed_commit"] = installed.get("resolved_commit")
            result["installed_archive_sha256"] = installed.get("archive_sha256")
        result["warnings"] = []
        if record["scripts_declared"]:
            result["warnings"].append("Package scripts are metadata only and were not executed.")
        if record["dependencies"]:
            result["warnings"].append("Dependencies are recorded as pending and were not installed.")
        if record["requested_permissions"]:
            result["warnings"].append("Requested permissions are informational and were not granted.")
        return result

    def check_publisher_evidence(
        self,
        selector: str,
        ref: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Inspect GitHub commit-signature evidence without mutating source state."""
        source_url, catalog_entry, user_catalog_entry = self._resolve_selector(selector)
        requested_ref, resolved_commit, _catalog_verified = self._resolve_requested_ref(
            source_url,
            catalog_entry,
            user_catalog_entry,
            ref,
        )
        subject = PublisherEvidenceSubject.create(source_url, resolved_commit)
        try:
            evidence = self.publisher_evidence_adapter.check(subject)
            if not isinstance(evidence, PublisherEvidenceResult) or evidence.subject != subject:
                raise PublisherEvidenceError(
                    "publisher evidence adapter returned a different subject",
                    "publisher_subject_mismatch",
                )
            self.publisher_evidence_store.write(evidence)
        except PublisherEvidenceError as exc:
            raise PluginManagerError(str(exc)) from exc
        if catalog_entry:
            result: Dict[str, Any] = catalog_entry.to_dict()
            result.update(self._curated_display_fields(catalog_entry))
        elif user_catalog_entry:
            result = dict(user_catalog_entry)
            result.update(trust_fields_for_record(result))
        else:
            result = {
                "id": _derived_id(source_url),
                "name": source_url.rsplit("/", 1)[-1],
                "source_url": source_url,
                "catalog_origin": "none",
                "catalog_auth_status": "unsigned",
                "policy_status": "unreviewed",
                "publisher_auth_status": "not_checked",
                "execution_enabled": False,
                "execution_status": "disabled",
            }
        result.update(
            {
                "source_url": source_url,
                "requested_ref": requested_ref,
                "resolved_commit": resolved_commit,
                "integrity_status": result.get("integrity_status", "unknown"),
                "integrity": result.get("integrity", "unknown"),
                "execution_enabled": False,
                "execution_status": "disabled",
            }
        )
        result.update(self._publisher_evidence_fields(result))
        result["publisher_evidence_checked"] = True
        return result

    def _installed_status(
        self,
        record: Mapping[str, Any],
        registry_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        result = dict(record)
        result.update(
            trust_fields_for_record(
                {
                    **result,
                    "integrity_status": result.get("integrity_status", "unknown"),
                    "catalog_auth_status": "unsigned",
                    "publisher_auth_status": "not_checked",
                    "policy_status": "unreviewed",
                    "catalog_key_id": None,
                }
            )
        )
        for stale_field in (
            "catalog_sequence",
            "signed_catalog_id",
            "catalog_issued_at",
            "catalog_expires_at",
            "catalog_endpoint",
            "catalog_trust_root_fingerprint",
            "catalog_keyset_id",
            "catalog_keyset_sequence",
            "catalog_keyset_expires_at",
            "catalog_keyset_active_key_ids",
            "catalog_keyset_revoked_key_ids",
            "catalog_trust_lifecycle_reason",
            "catalog_trust_last_transition",
            "catalog_freshness",
            "catalog_source",
        ):
            result.pop(stale_field, None)
        plugin_id = str(registry_id or record.get("id", ""))
        authoritative_files = record.get("integrity_files")
        result.pop("integrity_files", None)
        if not _ID_RE.fullmatch(plugin_id) or str(record.get("id", "")) != plugin_id:
            result["id"] = plugin_id
            result["integrity"] = "invalid-id"
            result["integrity_status"] = "corrupt"
            result["status"] = "corrupt"
            result["execution_enabled"] = False
            result.update(self._publisher_evidence_fields(result))
            return result
        if not isinstance(authoritative_files, dict):
            result["integrity"] = "unverifiable"
            result["integrity_status"] = "corrupt"
            result["status"] = "corrupt"
            result["execution_enabled"] = False
            result.update(self._publisher_evidence_fields(result))
            return result
        try:
            plugin_dir = self._managed_plugin_path(plugin_id)
        except PluginManagerError:
            result["integrity"] = "invalid-path"
            result["integrity_status"] = "corrupt"
            result["status"] = "corrupt"
            result["execution_enabled"] = False
            result.update(self._publisher_evidence_fields(result))
            return result
        integrity_path = plugin_dir / "integrity.json"
        source_root_value = str(record.get("source_root", ""))
        source_root = (plugin_dir / source_root_value).resolve()
        try:
            source_root.relative_to(plugin_dir.resolve())
        except ValueError:
            result["integrity"] = "invalid-path"
            result["integrity_status"] = "corrupt"
            result["status"] = "corrupt"
            result["execution_enabled"] = False
            result.update(self._publisher_evidence_fields(result))
            return result
        if (
            _is_link_or_reparse(integrity_path)
            or _is_link_or_reparse(source_root)
            or not plugin_dir.is_dir()
            or not integrity_path.is_file()
            or not source_root.is_dir()
        ):
            result["integrity"] = "missing"
            result["integrity_status"] = "corrupt"
            result["status"] = "missing"
            result["execution_enabled"] = False
            result.update(self._publisher_evidence_fields(result))
            return result
        try:
            integrity = _strict_json_loads(integrity_path.read_text(encoding="utf-8"))
            expected = integrity.get("files", {})
            actual = self._file_hashes(source_root)
            integrity_payload = {
                "version": integrity.get("version"),
                "algorithm": integrity.get("algorithm"),
                "archive_sha256": integrity.get("archive_sha256"),
                "manifest_sha256": integrity.get("manifest_sha256"),
                "source_root": integrity.get("source_root"),
                "files": expected,
            }
            valid = (
                expected == authoritative_files
                and expected == actual
                and integrity.get("archive_sha256") == record.get("archive_sha256")
                and integrity.get("manifest_sha256") == record.get("manifest_sha256")
                and integrity.get("source_root") == record.get("source_root")
                and record.get("integrity_manifest_sha256") == _sha256_bytes(_canonical_json(integrity_payload))
            )
        except Exception:
            valid = False
        result["integrity"] = "verified" if valid else "mismatch"
        result["integrity_status"] = "verified" if valid else "mismatch"
        if not valid:
            result["trust_reason_code"] = "installed_file_digest_mismatch"
        result["status"] = "installed" if valid else "corrupt"
        result["execution_enabled"] = False
        curated_id = str(record.get("catalog_id", "")) if record.get("curated") else ""
        curated_entry = next((entry for entry in CURATED_CATALOG if entry.id == curated_id), None)
        if curated_entry is not None:
            trust_record, _verification = self._verify_curated_record(curated_entry, result, refresh=False)
            result.update(trust_record)
        result.update(self._publisher_evidence_fields(result))
        return result

    def list_installed(self) -> List[Dict[str, Any]]:
        """List local records and re-check source integrity without importing files."""
        with self._operation_lock():
            records = self._read_registry()
            return [self._installed_status(records[key], key) for key in sorted(records)]

    def list_catalog(self, include_installed: bool = True, evidence_only: bool = False) -> List[Dict[str, Any]]:
        if evidence_only:
            return self._list_catalog_evidence_only(include_installed=include_installed)
        installed = {item["id"]: item for item in self.list_installed()} if include_installed else {}
        installed_by_url = {
            str(item.get("source_url")): item
            for item in installed.values()
            if item.get("source_url")
        }
        items: List[Dict[str, Any]] = []
        for entry in CURATED_CATALOG:
            item = entry.to_dict()
            item.update(self._curated_display_fields(entry))
            item.update(self._publisher_evidence_fields(item))
            item["catalog_present"] = True
            item["installed"] = entry.id in installed
            item["status"] = installed.get(entry.id, {}).get("status", "available")
            if entry.id in installed:
                item.update(
                    {
                        "resolved_commit": installed[entry.id].get("resolved_commit"),
                        "requested_ref": installed[entry.id].get("requested_ref"),
                        "archive_sha256": installed[entry.id].get("archive_sha256"),
                        "integrity": installed[entry.id].get("integrity"),
                    }
                )
            items.append(item)
        for _alias, record in self._read_catalog().items():
            item = dict(record)
            item["catalog_present"] = True
            installed_item = installed_by_url.get(str(record.get("source_url")))
            item["installed"] = bool(installed_item)
            item["status"] = installed_item.get("status", "catalog-only") if installed_item else "catalog-only"
            item["integrity"] = installed_item.get("integrity") if installed_item else None
            item.update(self._publisher_evidence_fields(item))
            if installed_item:
                item["installed_id"] = installed_item.get("id")
                item["installed_commit"] = installed_item.get("resolved_commit")
            items.append(item)
        for item in self.list_installed():
            if not any(
                existing.get("id") == item["id"]
                or existing.get("source_url") == item.get("source_url")
                for existing in items
            ):
                item["catalog_present"] = False
                items.append(item)
        # Keep explicitly saved user repositories first so a newly added
        # custom source remains the selected item in the TUI after a catalog
        # refresh.  Curated entries remain deterministic and alphabetical.
        return sorted(
            items,
            key=lambda item: (
                0 if item.get("catalog_origin") == "user" else 1,
                str(item.get("name", item.get("id", ""))).lower(),
            ),
        )

    def _list_catalog_evidence_only(self, *, include_installed: bool) -> List[Dict[str, Any]]:
        """Read catalog/registry metadata without lifecycle locks or recovery."""
        registry = self._read_registry() if include_installed and self.registry_path.exists() else {}
        installed_by_url = {
            str(item.get("source_url")): item
            for item in registry.values()
            if isinstance(item, dict) and item.get("source_url")
        }
        items: List[Dict[str, Any]] = []
        for entry in CURATED_CATALOG:
            item = entry.to_dict()
            item.update(self._curated_display_fields(entry))
            item.update(self._publisher_evidence_fields(item))
            installed = registry.get(entry.id)
            item["catalog_present"] = True
            item["installed"] = isinstance(installed, dict)
            item["status"] = "installed" if isinstance(installed, dict) else "available"
            if isinstance(installed, dict):
                item["resolved_commit"] = installed.get("resolved_commit")
                item["requested_ref"] = installed.get("requested_ref")
                item["archive_sha256"] = installed.get("archive_sha256")
                item["integrity"] = "not_checked"
                item["integrity_status"] = "not_checked"
            items.append(item)
        for _alias, record in self._read_catalog().items():
            item = dict(record)
            installed_item = installed_by_url.get(str(record.get("source_url")))
            item["catalog_present"] = True
            item["installed"] = bool(installed_item)
            item["status"] = "installed" if installed_item else "catalog-only"
            item["integrity"] = "not_checked" if installed_item else None
            item["integrity_status"] = "not_checked" if installed_item else "unknown"
            item.update(self._publisher_evidence_fields(item))
            items.append(item)
        if include_installed:
            catalog_urls = {str(item.get("source_url")) for item in items if item.get("source_url")}
            for plugin_id, record in registry.items():
                if not isinstance(record, dict) or str(record.get("source_url")) in catalog_urls:
                    continue
                item = dict(record)
                item["catalog_present"] = False
                item["installed"] = True
                item["status"] = "installed"
                item["integrity"] = "not_checked"
                item["integrity_status"] = "not_checked"
                item["execution_enabled"] = False
                item["execution_status"] = "disabled"
                item.update(self._publisher_evidence_fields(item))
                items.append(item)
        return sorted(items, key=lambda item: str(item.get("name", item.get("id", ""))).lower())

    def catalog_list(self) -> List[Dict[str, Any]]:
        """Return only curated and explicitly saved catalog records."""
        return [
            item
            for item in self.list_catalog()
            if item.get("catalog_origin") in {"curated", "user"}
        ]

    def search(self, query: str) -> List[Dict[str, Any]]:
        needle = query.strip().lower()
        if not needle:
            return self.list_catalog()
        return [
            item
            for item in self.list_catalog()
            if needle in " ".join(
                str(item.get(field, ""))
                for field in (
                    "id",
                    "name",
                    "description",
                    "category",
                    "author",
                    "source_url",
                    "language",
                    "topics",
                    "license_id",
                    "license_name",
                    "compatibility_status",
                )
            ).lower()
        ]

    def info(self, selector: str) -> Optional[Dict[str, Any]]:
        with self._operation_lock():
            records = self._read_registry()
            if selector in records:
                return self._installed_status(records[selector], selector)
            for entry in CURATED_CATALOG:
                if selector == entry.id:
                    item = entry.to_dict()
                    item.update(self._curated_display_fields(entry))
                    item.update(self._publisher_evidence_fields(item))
                    installed = records.get(selector)
                    item["installed"] = bool(installed)
                    if installed:
                        item.update(self._installed_status(installed, selector))
                    return item
            user_catalog = self._read_catalog()
            if selector in user_catalog:
                item = dict(user_catalog[selector])
                item.update(self._publisher_evidence_fields(item))
                installed = next(
                    (
                        self._installed_status(records[key], key)
                        for key in sorted(records)
                        if records[key].get("source_url") == item.get("source_url")
                    ),
                    None,
                )
                item["installed"] = bool(installed)
                if installed:
                    item["installed_id"] = installed.get("id")
                    item["integrity"] = installed.get("integrity")
                    item["status"] = installed.get("status")
                    item["installed_commit"] = installed.get("resolved_commit")
                return item
        try:
            source_url = canonicalize_github_url(selector)
        except PluginManagerError:
            return None
        return next((item for item in self.list_catalog() if item.get("source_url") == source_url), None)

    def _resolve_installed_id(
        self,
        selector: str,
        records: Mapping[str, Mapping[str, Any]],
    ) -> str:
        """Resolve an installed plugin ID or a saved catalog alias safely."""
        candidate = _safe_id(selector)
        if candidate in records:
            return candidate
        user_entry = self._read_catalog().get(candidate)
        if not user_entry:
            return candidate
        matches = [
            record_id
            for record_id, record in records.items()
            if record.get("source_url") == user_entry.get("source_url")
        ]
        if len(matches) > 1:
            raise PluginManagerError(
                f"Catalog alias '{candidate}' matches multiple installed source IDs; use the installed ID."
            )
        return matches[0] if matches else candidate

    def catalog_add(
        self,
        alias: str,
        source: str,
        ref: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persist a user repository pin without installing its source."""
        alias = self._catalog_id(alias)
        source_url = canonicalize_github_url(source)
        if any(entry.source_url == source_url for entry in CURATED_CATALOG):
            raise PluginManagerError("Curated repositories must be referenced by their protected curated ID.")
        with self._operation_lock():
            existing_records = self._read_catalog()
            if alias in existing_records:
                raise PluginManagerError(f"Catalog ID '{alias}' already exists.")
        repository_metadata = self._fetch_repository_metadata(source_url)
        requested_ref, _resolved, _ = self._resolve_requested_ref(source_url, None, None, ref)
        inspected = self.inspect(source_url, ref=requested_ref, repository_metadata=repository_metadata)
        now = time.time()
        record = dict(repository_metadata)
        record.update(
            {
                "id": alias,
                "source_url": source_url,
                "source_type": "user",
                "catalog_origin": "user",
                "source_plugin_id": inspected["id"],
                "requested_ref": requested_ref,
                "resolved_commit": inspected["resolved_commit"],
                "observed_commit": inspected["resolved_commit"],
                "description": repository_metadata.get("description") or inspected.get("description", ""),
                "name": repository_metadata.get("name") or inspected.get("name", alias),
                "version": inspected.get("version", "0.0.0"),
                "author": inspected.get("author", "community"),
                "license": inspected.get("license", ""),
                "manifest_file": inspected.get("manifest_file"),
                "compatibility": inspected.get("compatibility", {}),
                "compatibility_status": inspected.get("compatibility_status", "unknown"),
                "compatibility_reasons": inspected.get("compatibility_reasons", []),
                "last_inspected_at": now,
                "last_refreshed_at": now,
                "update_available": False,
                "last_error": None,
                "execution_enabled": False,
                "installed": False,
            }
        )
        record.update(trust_fields_for_record(inspected))
        with self._operation_lock():
            records = self._read_catalog()
            if alias in records:
                raise PluginManagerError(f"Catalog ID '{alias}' already exists.")
            if any(record.get("source_url") == source_url for record in records.values()):
                raise PluginManagerError("That repository is already saved in the user catalog.")
            records[alias] = record
            self._write_catalog(records)
        return record

    def catalog_remove(self, alias: str) -> bool:
        """Remove only a user catalog record, never installed source data."""
        alias = self._catalog_id(alias)
        with self._operation_lock():
            records = self._read_catalog()
            if alias not in records:
                return False
            del records[alias]
            self._write_catalog(records)
            return True

    def catalog_refresh(self, alias: str) -> Dict[str, Any]:
        """Observe a saved ref without changing its selected immutable pin."""
        alias = self._catalog_id(alias)
        with self._operation_lock():
            records = self._read_catalog()
            current = records.get(alias)
            if not current:
                raise PluginManagerError(f"Catalog ID '{alias}' is not saved.")
        source_url = str(current["source_url"])
        requested_ref = validate_ref(str(current.get("requested_ref", DEFAULT_REF)))
        candidate: Optional[Dict[str, Any]] = None
        candidate_commit: Optional[str] = None
        error: Optional[str] = None
        try:
            repository_metadata = self._fetch_repository_metadata(source_url)
            _requested, candidate_commit, _ = self._resolve_requested_ref(
                source_url, None, None, requested_ref
            )
            candidate = self.inspect(
                source_url,
                ref=requested_ref,
                repository_metadata=repository_metadata,
            )
        except PluginManagerError as exc:
            error = str(exc)

        with self._operation_lock():
            records = self._read_catalog()
            latest = records.get(alias)
            if not latest:
                raise PluginManagerError(f"Catalog ID '{alias}' was removed during refresh; retry.")
            if (
                latest.get("source_url") != current.get("source_url")
                or latest.get("resolved_commit") != current.get("resolved_commit")
            ):
                raise PluginManagerError(f"Catalog ID '{alias}' changed during refresh; retry.")
            record = dict(latest)
            record["last_refreshed_at"] = time.time()
            record["last_error"] = error
            if candidate_commit:
                record["observed_commit"] = candidate_commit
                record["candidate_commit"] = candidate_commit
                record["update_available"] = candidate_commit != record["resolved_commit"]
            if candidate:
                record["candidate_compatibility"] = candidate.get("compatibility", {})
                record["candidate_compatibility_status"] = candidate.get("compatibility_status", "unknown")
                record["candidate_metadata"] = {
                    "name": candidate.get("name"),
                    "version": candidate.get("version"),
                    "description": candidate.get("description"),
                    "manifest_file": candidate.get("manifest_file"),
                }
            records[alias] = record
            self._write_catalog(records)
            return record

    def catalog_refresh_all(self) -> List[Dict[str, Any]]:
        with self._operation_lock():
            aliases = sorted(self._read_catalog())
        return [self.catalog_refresh(alias) for alias in aliases]

    def catalog_pin(self, alias: str, ref: str) -> Dict[str, Any]:
        """Explicitly replace a user entry's selected ref and immutable pin."""
        alias = self._catalog_id(alias)
        requested_ref = validate_ref(ref)
        with self._operation_lock():
            current = self._read_catalog().get(alias)
            if not current:
                raise PluginManagerError(f"Catalog ID '{alias}' is not saved.")
        source_url = str(current["source_url"])
        repository_metadata = self._fetch_repository_metadata(source_url)
        inspected = self.inspect(
            source_url,
            ref=requested_ref,
            repository_metadata=repository_metadata,
            allow_user_catalog_ref=True,
        )
        updated = dict(current)
        updated.update(
            {
                "requested_ref": requested_ref,
                "resolved_commit": inspected["resolved_commit"],
                "source_plugin_id": inspected["id"],
                "observed_commit": inspected["resolved_commit"],
                "candidate_commit": None,
                "candidate_metadata": None,
                "update_available": False,
                "compatibility": inspected.get("compatibility", {}),
                "compatibility_status": inspected.get("compatibility_status", "unknown"),
                "compatibility_reasons": inspected.get("compatibility_reasons", []),
                "last_inspected_at": time.time(),
                "last_refreshed_at": time.time(),
                "last_error": None,
            }
        )
        with self._operation_lock():
            records = self._read_catalog()
            latest = records.get(alias)
            if not latest:
                raise PluginManagerError(f"Catalog ID '{alias}' was removed during pin; retry.")
            if (
                latest.get("source_url") != current.get("source_url")
                or latest.get("resolved_commit") != current.get("resolved_commit")
            ):
                raise PluginManagerError(f"Catalog ID '{alias}' changed during pin; retry.")
            records[alias] = updated
            self._write_catalog(records)
        return updated

    def discover(
        self,
        query: str,
        limit: int = 20,
        language: Optional[str] = None,
        min_stars: int = 0,
        include_archived: bool = False,
    ) -> List[Dict[str, Any]]:
        """Search GitHub repositories without saving, downloading, or executing them."""
        query = _clean_text(query, "query", 256)
        if not query:
            raise PluginManagerError("Discovery query cannot be empty.")
        if limit < 1 or limit > 25:
            raise PluginManagerError("Discovery limit must be between 1 and 25.")
        if min_stars < 0:
            raise PluginManagerError("Minimum stars cannot be negative.")
        qualifiers = [query]
        if language:
            qualifiers.append(f"language:{_clean_text(language, 'language', 64)}")
        if min_stars:
            qualifiers.append(f"stars:>={min_stars}")
        if not include_archived:
            qualifiers.append("archived:false")
        url = (
            "https://api.github.com/search/repositories?q="
            + quote_plus(" ".join(qualifiers))
            + f"&sort=stars&order=desc&per_page={limit}&page=1"
        )
        data = self._json_fetcher(url)
        if not isinstance(data, Mapping) or not isinstance(data.get("items"), list):
            raise PluginManagerError("GitHub discovery response was malformed.")
        results: List[Dict[str, Any]] = []
        for item in data["items"]:
            if not isinstance(item, Mapping):
                continue
            raw_url = item.get("html_url") or item.get("clone_url")
            if not raw_url:
                continue
            try:
                source_url = canonicalize_github_url(str(raw_url))
            except PluginManagerError:
                continue
            repo_license = item.get("license") if isinstance(item.get("license"), Mapping) else {}
            license_id = _clean_text(repo_license.get("spdx_id"), "license.spdx_id", 128)
            license_name = _clean_text(repo_license.get("name"), "license.name", 256)
            archived = bool(item.get("archived", False))
            if archived and not include_archived:
                continue
            results.append(
                {
                    "id": _derived_id(str(item.get("full_name") or item.get("name") or source_url.rsplit("/", 1)[-1])),
                    "name": _clean_text(item.get("name"), "name", 256),
                    "full_name": _clean_text(item.get("full_name"), "full_name", 256),
                    "source_url": source_url,
                    "description": _clean_text(item.get("description"), "description", 4096),
                    "language": _clean_text(item.get("language"), "language", 128),
                    "topics": [
                        _clean_text(topic, "topic", 128)
                        for topic in (item.get("topics") or [])
                        if isinstance(topic, str)
                    ][:50],
                    "stars": _safe_count(item.get("stargazers_count"), "stargazers_count"),
                    "forks": _safe_count(item.get("forks_count"), "forks_count"),
                    "license_id": license_id,
                    "license_name": license_name,
                    "license_declared": bool(license_id or license_name),
                    "archived": archived,
                    "pushed_at": _clean_text(item.get("pushed_at"), "pushed_at", 128),
                    "updated_at": _clean_text(item.get("updated_at"), "updated_at", 128),
                    "default_branch": _clean_text(item.get("default_branch"), "default_branch", 256),
                    "resolved_commit": None,
                    "compatibility_status": "unknown",
                    "compatibility_reasons": ["discovery_not_inspected"],
                    "source_type": "discovery",
                    "catalog_origin": "discovery",
                    "execution_enabled": False,
                    "integrity_status": "unknown",
                    "catalog_auth_status": "unsigned",
                    "publisher_auth_status": "not_checked",
                    "policy_status": "unreviewed",
                    "execution_status": "disabled",
                    "trust_reason_code": None,
                    "catalog_key_id": None,
                    "installed": False,
                }
            )
        return results

    def install(
        self,
        selector: str,
        ref: Optional[str] = None,
        expected_sha256: Optional[str] = None,
        replace: bool = False,
        expected_plugin_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Serialize and recoverably install a quarantined source."""
        with self._operation_lock():
            return self._install_locked(
                selector,
                ref=ref,
                expected_sha256=expected_sha256,
                replace=replace,
                expected_plugin_id=expected_plugin_id,
            )

    def _install_locked(
        self,
        selector: str,
        ref: Optional[str] = None,
        expected_sha256: Optional[str] = None,
        replace: bool = False,
        expected_plugin_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Install a quarantined source atomically and return its metadata record."""
        source_url, catalog_entry, user_catalog_entry = self._resolve_selector(selector)
        requested_ref, resolved_commit, _catalog_verified = self._resolve_requested_ref(
            source_url, catalog_entry, user_catalog_entry, ref
        )
        archive_url = self._archive_url(source_url, resolved_commit)
        archive = self._bytes_fetcher(archive_url)
        if not isinstance(archive, bytes):
            raise PluginManagerError("Archive fetcher returned a non-bytes value.")
        archive_sha256 = _sha256_bytes(archive)
        if expected_sha256:
            expected = expected_sha256.strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise PluginManagerError("Expected archive SHA-256 must be 64 hexadecimal characters.")
            if expected != archive_sha256:
                raise PluginManagerError("Downloaded archive SHA-256 does not match --expected-sha256.")

        registry = self._read_registry()
        # We need the source manifest to determine the ID before deciding
        # whether this is an idempotent install or an update.
        temp_dir = Path(tempfile.mkdtemp(dir=str(self.staging_dir), prefix="install-"))
        candidate_source = temp_dir / "source"
        try:
            preview, _root, file_hashes = self._archive_metadata(
                archive,
                source_url,
                requested_ref,
                resolved_commit,
                catalog_entry,
                candidate_source,
                user_catalog_entry=user_catalog_entry,
            )
            plugin_id = _safe_id(str(preview["id"]))
            if expected_plugin_id and plugin_id != expected_plugin_id:
                raise PluginManagerError(
                    f"Updated source ID '{plugin_id}' does not match installed plugin '{expected_plugin_id}'."
                )
            if user_catalog_entry:
                catalog_source_id = str(user_catalog_entry.get("source_plugin_id", ""))
                if not _ID_RE.fullmatch(catalog_source_id) or plugin_id != catalog_source_id:
                    raise PluginManagerError(
                        f"Saved catalog source ID '{catalog_source_id}' does not match fetched source ID '{plugin_id}'."
                    )
            if catalog_entry:
                preview.update(self._require_curated_approval(catalog_entry, preview))
            existing = registry.get(plugin_id)
            if existing and not replace:
                if existing.get("archive_sha256") == archive_sha256:
                    result = self._installed_status(existing, plugin_id)
                    if result.get("integrity") == "verified":
                        result["idempotent"] = True
                        return result
                    # Repair legacy or tampered records through a fresh
                    # atomic promotion rather than claiming idempotency.
                    replace = True
                else:
                    raise PluginManagerError(
                        f"Plugin '{plugin_id}' is already installed with a different source. Use 'plugins update'."
                    )

            preview["status"] = "installed"
            preview["integrity"] = "verified"
            preview["installed"] = True
            preview["installed_at"] = time.time()
            preview["updated_at"] = preview["installed_at"]
            preview["source_root"] = f"source/{preview['source_root']}"
            preview["execution_enabled"] = False
            preview.update(trust_fields_for_record(preview))
            preview["integrity_status"] = "verified"
            preview["trusted"] = False
            preview["dependencies_pending"] = list(preview.get("dependencies", []))
            preview["permissions_granted"] = []
            preview["lifecycle"] = "metadata-only"
            integrity_payload = {
                "version": 1,
                "algorithm": "sha256",
                "archive_sha256": archive_sha256,
                "manifest_sha256": preview["manifest_sha256"],
                "source_root": preview["source_root"],
                "files": file_hashes,
            }
            preview["integrity_files"] = dict(file_hashes)
            preview["integrity_manifest_sha256"] = _sha256_bytes(_canonical_json(integrity_payload))

            _atomic_json_write(
                temp_dir / "metadata.json",
                preview,
            )
            _atomic_json_write(
                temp_dir / "integrity.json",
                integrity_payload,
            )

            target = self._managed_plugin_path(plugin_id)
            if target.exists() and not target.is_dir():
                raise PluginManagerError(f"Managed plugin target '{plugin_id}' is not a directory.")
            backup: Optional[Path] = None
            candidate_relative = _relative_file(self.root, temp_dir)
            journal: Dict[str, Any] = {
                "version": 1,
                "operation": "install",
                "phase": "prepared",
                "plugin_id": plugin_id,
                "candidate": candidate_relative,
                "backup": None,
                "new_archive_sha256": archive_sha256,
            }
            if target.exists():
                if not replace:
                    raise PluginManagerError(f"Plugin '{plugin_id}' is already installed.")
                backup = self.staging_dir / f"backup-{plugin_id}-{uuid.uuid4().hex}"
                journal["backup"] = _relative_file(self.root, backup)
            self._write_journal(journal)
            try:
                if backup:
                    journal["phase"] = "backup-pending"
                    self._write_journal(journal)
                    os.replace(str(target), str(backup))
                    journal["phase"] = "backup-moved"
                    self._write_journal(journal)
                journal["phase"] = "promotion-pending"
                self._write_journal(journal)
                os.replace(str(temp_dir), str(target))
                journal["phase"] = "promoted"
                self._write_journal(journal)
                new_registry = dict(registry)
                new_registry[plugin_id] = preview
                journal["phase"] = "registry-pending"
                self._write_journal(journal)
                self._write_registry(new_registry)
                journal["phase"] = "registry-published"
                self._write_journal(journal)
            except BaseException:
                self._recover_transaction()
                raise
            else:
                if backup and backup.exists():
                    shutil.rmtree(backup, ignore_errors=True)
                self._clear_journal()
            return self._installed_status(preview)
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

    def update(
        self,
        plugin_id: str,
        source: Optional[str] = None,
        ref: Optional[str] = None,
        expected_sha256: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Replace an installed record while requiring the same plugin ID."""
        selector = _safe_id(plugin_id)
        with self._operation_lock():
            registry = self._read_registry()
            resolved_id = self._resolve_installed_id(selector, registry)
            existing = registry.get(resolved_id)
            if not existing:
                raise PluginManagerError(f"Plugin '{selector}' is not installed.")
            catalog_id = str(existing.get("catalog_id", "")) if existing.get("catalog_origin") == "user" else ""
            catalog_records = self._read_catalog()
            saved_catalog = catalog_records.get(catalog_id) if catalog_id else None
            if saved_catalog:
                if source is not None or ref is not None:
                    raise PluginManagerError(
                        "Saved catalog sources update only after an explicit 'plugins catalog pin'; "
                        "do not override the selected pin with plugins update."
                    )
                install_selector = catalog_id
                requested_ref = None
            else:
                install_selector = source or (
                    selector if existing.get("curated") and existing.get("catalog_id") == selector
                    else str(existing.get("source_url", ""))
                )
                if not install_selector:
                    raise PluginManagerError(f"Plugin '{selector}' has no recorded source URL.")
                requested_ref = ref if ref is not None else str(existing.get("requested_ref") or DEFAULT_REF)
            return self._install_locked(
                install_selector,
                ref=requested_ref,
                expected_sha256=expected_sha256,
                replace=True,
                expected_plugin_id=resolved_id,
            )

    def status(self, plugin_id: Optional[str] = None) -> Any:
        """Return integrity status for one installed source or all sources."""
        with self._operation_lock():
            records = self._read_registry()
            if plugin_id:
                plugin_id = self._resolve_installed_id(plugin_id, records)
                record = records.get(plugin_id)
                if not record:
                    raise PluginManagerError(f"Plugin '{plugin_id}' is not installed.")
                return self._installed_status(record, plugin_id)
            return [self._installed_status(records[key], key) for key in sorted(records)]

    def remove(self, plugin_id: str) -> bool:
        """Remove only a managed plugin directory and its registry record."""
        selector = _safe_id(plugin_id)
        with self._operation_lock():
            records = self._read_registry()
            plugin_id = self._resolve_installed_id(selector, records)
            if plugin_id not in records:
                return False
            target = self._managed_plugin_path(plugin_id)
            if target.exists() and not target.is_dir():
                raise PluginManagerError(f"Managed plugin target '{plugin_id}' is not a directory.")
            backup = self.staging_dir / f"remove-{plugin_id}-{uuid.uuid4().hex}"
            journal: Dict[str, Any] = {
                "version": 1,
                "operation": "remove",
                "phase": "prepared",
                "plugin_id": plugin_id,
                "candidate": None,
                "backup": _relative_file(self.root, backup),
                "new_archive_sha256": None,
            }
            self._write_journal(journal)
            try:
                if target.exists():
                    journal["phase"] = "backup-pending"
                    self._write_journal(journal)
                    os.replace(str(target), str(backup))
                    journal["phase"] = "backup-moved"
                    self._write_journal(journal)
                updated = dict(records)
                del updated[plugin_id]
                journal["phase"] = "registry-pending"
                self._write_journal(journal)
                self._write_registry(updated)
                journal["phase"] = "registry-published"
                self._write_journal(journal)
            except BaseException:
                self._recover_transaction()
                raise
            else:
                if backup.exists():
                    shutil.rmtree(backup, ignore_errors=True)
                self._clear_journal()
            return True


__all__ = [
    "CURATED_CATALOG",
    "CatalogEntry",
    "PluginCatalogManager",
    "PluginManagerError",
    "canonicalize_github_url",
    "validate_ref",
]

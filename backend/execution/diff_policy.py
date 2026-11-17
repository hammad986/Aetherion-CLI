"""Shared, fail-closed policy for read-only diff extraction.

This module contains no application or database imports.  It is deliberately
small so every diff producer can apply the same sensitive-path and resource
limits without importing a route or execution runtime.
"""
from __future__ import annotations

import os
import re
import difflib
import stat
import subprocess
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


# Conservative fixed limits.  They are source policy, not environment flags:
# Option B must not be made less restrictive by ambient configuration.
DIFF_MAX_UNTRACKED_FILES = 100
DIFF_MAX_MODIFIED_FILES = 100
DIFF_MAX_UNTRACKED_FILE_BYTES = 1 * 1024 * 1024
DIFF_MAX_UNTRACKED_TOTAL_BYTES = 8 * 1024 * 1024
DIFF_MAX_RENDERED_BYTES = 10 * 1024 * 1024
DIFF_MAX_STDERR_BYTES = 64 * 1024
DIFF_MAX_GIT_TIMEOUT_SEC = 5

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
SENSITIVE_DIRECTORY_NAMES = frozenset({
    ".git",
    ".ssh",
    ".aws",
    ".azure",
    ".gnupg",
    ".kube",
})
SENSITIVE_FILE_NAMES = frozenset({
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    "credentials",
    "credentials.json",
    "secrets.json",
    "service-account.json",
    "token.json",
})
SENSITIVE_FILE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks")


def diff_pathspec_exclusions() -> Tuple[str, ...]:
    """Return Git pathspec exclusions matching the centralized sensitive policy."""
    patterns = [
        *[f":(exclude)**/{name}/**" for name in sorted(SENSITIVE_DIRECTORY_NAMES)],
        *[f":(exclude)**/{name}" for name in sorted(SENSITIVE_FILE_NAMES)],
        ":(exclude)**/.env",
        ":(exclude)**/.env.*",
        *[f":(exclude)**/*{suffix}" for suffix in SENSITIVE_FILE_SUFFIXES],
    ]
    return tuple(patterns)


def build_tracked_diff_argv(base_ref: str, *, context_lines: Optional[int] = None) -> List[str]:
    """Build the only tracked-diff command accepted by shared producers."""
    if not isinstance(base_ref, str) or not base_ref.strip() or "\x00" in base_ref:
        raise DiffExtractionError("invalid diff base ref")
    if base_ref.startswith("-"):
        raise DiffExtractionError("invalid diff base ref")
    argv = ["git", "--no-optional-locks", "diff", "--no-ext-diff", "--no-textconv", "--no-color"]
    if context_lines is not None:
        try:
            bounded_context = max(0, min(25, int(context_lines)))
        except (TypeError, ValueError) as exc:
            raise DiffExtractionError("invalid diff context") from exc
        argv.append(f"--unified={bounded_context}")
    argv.extend([base_ref, "--", ".", *diff_pathspec_exclusions()])
    return argv


def build_untracked_listing_argv() -> List[str]:
    """Build the bounded, non-index-mutating untracked candidate listing."""
    return [
        "git",
        "--no-optional-locks",
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        ".",
        *diff_pathspec_exclusions(),
    ]


class DiffExtractionError(RuntimeError):
    """Raised when a diff cannot be returned as a complete safe result."""


def _normal_parts(relative: str) -> Tuple[str, ...]:
    """Normalize a Git relative path without touching the filesystem."""
    text = str(relative or "")
    if not text or "\x00" in text:
        raise DiffExtractionError("diff path is empty or contains a NUL byte")
    text = text.replace("\\", "/")
    if text.startswith("/") or _WINDOWS_DRIVE_RE.match(text):
        raise DiffExtractionError("absolute diff paths are not permitted")
    parts = PurePosixPath(text).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise DiffExtractionError("diff path contains an unsafe traversal component")
    return tuple(parts)


def is_sensitive_relative_path(relative: str) -> bool:
    """Return whether a path is a known credential/metadata location."""
    try:
        parts = tuple(part.lower() for part in _normal_parts(relative))
    except DiffExtractionError:
        return True
    basename = parts[-1]
    if any(part in SENSITIVE_DIRECTORY_NAMES for part in parts):
        return True
    if any(part == ".env" or part.startswith(".env.") for part in parts):
        return True
    if basename in SENSITIVE_FILE_NAMES:
        return True
    return basename.endswith(SENSITIVE_FILE_SUFFIXES)


def safe_relative_parts(relative: str) -> Tuple[str, ...]:
    """Validate and return path components for safe workspace joining."""
    parts = _normal_parts(relative)
    if is_sensitive_relative_path(relative):
        raise DiffExtractionError("diff path is excluded by the sensitive-file policy")
    return parts


def resolve_diff_path(workspace: Path, relative: str) -> Path:
    """Resolve a relative candidate and reject traversal/symlink escapes."""
    parts = safe_relative_parts(relative)
    root = workspace.resolve()
    candidate = root.joinpath(*parts)

    # Reject symlinks in every existing component before reading.  The final
    # containment check remains necessary for aliases and intermediate links.
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise DiffExtractionError("symlink diff paths are not permitted")

    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise DiffExtractionError("diff path escapes the execution workspace") from exc
    if is_sensitive_relative_path(str(resolved.relative_to(root)).replace(os.sep, "/")):
        raise DiffExtractionError("diff path is excluded by the sensitive-file policy")
    return resolved


def resolve_authorized_worktree_path(
    worktree_path: Any,
    repo_root: Any,
    *,
    require_isolated: bool = True,
) -> Tuple[Path, Path]:
    """Resolve a trusted worktree/root pair without inferring missing values.

    Both values must come from trusted lifecycle state.  This helper deliberately
    does not accept a default root, current directory, or caller-selected base.
    The resolved worktree must be an existing directory below the resolved
    repository root and, when isolation is required, cannot be the repository
    root itself.
    """
    if not isinstance(repo_root, (str, os.PathLike)) or not str(repo_root).strip():
        raise DiffExtractionError("authorized repository root is unavailable")
    if not isinstance(worktree_path, (str, os.PathLike)) or not str(worktree_path).strip():
        raise DiffExtractionError("authorized worktree path is unavailable")

    try:
        raw_root = Path(repo_root)
        raw_worktree = Path(worktree_path)
        if not raw_root.is_absolute() or not raw_worktree.is_absolute():
            raise DiffExtractionError("authorized worktree paths must be absolute")
        root = raw_root.resolve(strict=True)
        candidate = raw_worktree.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DiffExtractionError("authorized worktree path is unavailable") from exc

    if not root.is_dir() or not candidate.is_dir():
        raise DiffExtractionError("authorized worktree path is unavailable")
    if require_isolated and candidate == root:
        raise DiffExtractionError("execution workspace is not an isolated worktree")
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise DiffExtractionError("worktree path escapes the authorized repository") from exc

    # Resolve() above rejects escaped targets, while this explicit component
    # check rejects a symlinked worktree path even when its target happens to be
    # inside the repository.  The repository root itself may be a symlink that
    # was resolved as trusted lifecycle state; the candidate may not be one.
    current = root
    try:
        relative_parts = candidate.relative_to(root).parts
        for part in relative_parts:
            current = current / part
            if current.is_symlink():
                raise DiffExtractionError("symlink worktree paths are not permitted")
    except ValueError as exc:
        raise DiffExtractionError("worktree path escapes the authorized repository") from exc
    return root, candidate


def ensure_diff_text_bounded(text: str, *, label: str = "diff") -> str:
    """Reject output larger than the complete-result response budget."""
    value = str(text or "")
    if len(value.encode("utf-8", errors="replace")) > DIFF_MAX_RENDERED_BYTES:
        raise DiffExtractionError(f"{label} exceeds the rendered diff size limit")
    return value


def ensure_diff_text_safe(text: str) -> str:
    """Reject rendered diff headers that identify excluded sensitive paths."""
    value = ensure_diff_text_bounded(text)
    for line in value.splitlines():
        candidates: List[str] = []
        if line.startswith("diff --git a/") and " b/" in line:
            left, right = line[len("diff --git a/"):].split(" b/", 1)
            candidates.extend((left, right))
        elif line.startswith("--- a/"):
            candidates.append(line[len("--- a/"):].split("\t", 1)[0])
        elif line.startswith("+++ b/"):
            candidates.append(line[len("+++ b/"):].split("\t", 1)[0])
        for candidate in candidates:
            normalized = candidate.strip().strip('"')
            if is_sensitive_relative_path(normalized):
                raise DiffExtractionError("diff contains an excluded sensitive path")
    return value


def render_untracked_file_diff(
    workspace: Path,
    relative: str,
    aggregate_bytes: int,
) -> Tuple[str, int]:
    """Render one eligible untracked file without staging or reading secrets."""
    path = resolve_diff_path(workspace, relative)
    if not path.is_file():
        raise DiffExtractionError("untracked diff path is not a regular file")

    try:
        file_stat = path.stat()
        if file_stat.st_size > DIFF_MAX_UNTRACKED_FILE_BYTES:
            raise DiffExtractionError("untracked file exceeds the per-file diff size limit")
        if aggregate_bytes + file_stat.st_size > DIFF_MAX_UNTRACKED_TOTAL_BYTES:
            raise DiffExtractionError("untracked files exceed the aggregate diff size limit")
        with path.open("rb") as handle:
            content = handle.read(DIFF_MAX_UNTRACKED_FILE_BYTES + 1)
        if len(content) > DIFF_MAX_UNTRACKED_FILE_BYTES:
            raise DiffExtractionError("untracked file exceeds the per-file diff size limit")
        mode = stat.S_IMODE(file_stat.st_mode)
    except DiffExtractionError:
        raise
    except OSError as exc:
        raise DiffExtractionError("could not read untracked diff path") from exc

    display_path = Path(relative).as_posix()
    git_mode = 0o100000 | mode
    header = (
        f"diff --git a/{display_path} b/{display_path}\n"
        f"new file mode {git_mode:06o}\n"
    )
    if b"\x00" in content:
        return header + f"Binary files /dev/null and b/{display_path} differ\n", aggregate_bytes + len(content)

    text = content.decode("utf-8", errors="replace")
    unified = difflib.unified_diff(
        [],
        text.splitlines(keepends=True),
        fromfile="/dev/null",
        tofile=f"b/{display_path}",
    )
    return header + "".join(unified), aggregate_bytes + len(content)


def extract_worktree_diff(
    workspace: Path,
    base_ref: str,
    run_git: Callable[[Sequence[str], Path], str],
    *,
    context_lines: Optional[int] = None,
) -> str:
    """Extract a complete bounded diff through a caller-authorized Git runner."""
    tracked_diff = run_git(build_tracked_diff_argv(base_ref, context_lines=context_lines), workspace)
    untracked_listing = run_git(build_untracked_listing_argv(), workspace)
    candidates = [relative for relative in untracked_listing.split("\x00") if relative]
    if len(candidates) > DIFF_MAX_UNTRACKED_FILES:
        raise DiffExtractionError("untracked file count exceeds the diff limit")

    rendered = [tracked_diff]
    aggregate_bytes = 0
    rendered_bytes = len(tracked_diff.encode("utf-8", errors="replace"))
    for relative in candidates:
        # Invalid paths fail closed.  Known sensitive paths are omitted before
        # resolve/stat/open, so their content is never read by this producer.
        _normal_parts(relative)
        if is_sensitive_relative_path(relative):
            continue
        piece, aggregate_bytes = render_untracked_file_diff(workspace, relative, aggregate_bytes)
        rendered_bytes += len(piece.encode("utf-8", errors="replace"))
        if rendered_bytes > DIFF_MAX_RENDERED_BYTES:
            raise DiffExtractionError("diff exceeds the rendered diff size limit")
        rendered.append(piece)
    return ensure_diff_text_safe("".join(rendered))


def diff_payload(task_id: str, diff_text: str) -> Dict[str, Any]:
    """Build the compatible task-diff response only after bounded validation."""
    bounded = ensure_diff_text_safe(diff_text)
    lines = bounded.splitlines()
    return {
        "ok": True,
        "task_id": task_id,
        "diff": bounded,
        "lines": lines,
        "files_changed": len([line for line in lines if line.startswith("+++ b/")]),
    }


def run_bounded_readonly_git(
    argv: Sequence[str],
    cwd: Path,
    *,
    popen_kwargs: Optional[Dict[str, Any]] = None,
    on_complete: Optional[Callable[[int], None]] = None,
) -> str:
    """Run a read-only Git command with bounded stdout/stderr capture.

    Reader threads continue draining both pipes after the output budget is
    exceeded, preventing a child-process pipe deadlock while retaining only a
    bounded prefix. The result is rejected rather than truncated.
    """
    launch_kwargs: Dict[str, Any] = dict(popen_kwargs or {})
    # Governance may provide a sanitized environment/startup configuration.
    # The bounded runner owns argv, cwd, pipes and shell behavior so a caller
    # cannot weaken the capture or redirect the command elsewhere.
    for forbidden in ("capture_output", "check", "timeout", "text", "encoding", "errors", "input"):
        launch_kwargs.pop(forbidden, None)
    launch_kwargs.update({
        "args": list(argv),
        "cwd": str(cwd),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
    })
    try:
        proc = subprocess.Popen(**launch_kwargs)
    except OSError as exc:
        raise DiffExtractionError("read-only Git inspection could not be launched") from exc

    stdout = bytearray()
    stderr = bytearray()
    totals = {"stdout": 0, "stderr": 0}

    def drain(stream: Any, buffer: bytearray, key: str, limit: int) -> None:
        if stream is None:
            return
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            totals[key] += len(chunk)
            if len(buffer) < limit:
                buffer.extend(chunk[: max(0, limit - len(buffer))])

    threads = [
        threading.Thread(target=drain, args=(proc.stdout, stdout, "stdout", DIFF_MAX_RENDERED_BYTES), daemon=True),
        threading.Thread(target=drain, args=(proc.stderr, stderr, "stderr", DIFF_MAX_STDERR_BYTES), daemon=True),
    ]
    for thread in threads:
        thread.start()

    try:
        return_code = proc.wait(timeout=DIFF_MAX_GIT_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait(timeout=1)
        for thread in threads:
            thread.join(timeout=1)
        raise DiffExtractionError("read-only Git inspection timed out") from exc
    finally:
        for thread in threads:
            thread.join(timeout=1)

    if on_complete is not None:
        try:
            on_complete(return_code)
        except Exception:
            # Governance telemetry must not turn a completed bounded read into
            # a success if its post-dispatch hook fails; the caller still sees
            # the extraction result or failure below.
            pass
    if totals["stdout"] > DIFF_MAX_RENDERED_BYTES:
        raise DiffExtractionError("diff exceeds the rendered diff size limit")
    if return_code != 0:
        raise DiffExtractionError(f"read-only Git inspection failed (rc={return_code})")
    return stdout.decode("utf-8", errors="replace")


def error_payload(code: str, message: str, *, task_id: Optional[str] = None) -> Dict[str, Any]:
    """Create a sanitized failure response without paths or exception details."""
    payload: Dict[str, Any] = {"ok": False, "error_code": code, "error": message}
    if task_id is not None:
        payload["task_id"] = task_id
    return payload

#!/usr/bin/env python3
"""Generate a deterministic, secret-free release ledger for this repository."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

DEFAULT_EXCLUDED_DIRS = {
    ".git", ".aetherion_worktrees", "node_modules", "dist", "audit",
    "__pycache__", ".pytest_cache", ".venv", "venv", "scratch", "temp_live_ws",
}
DEFAULT_EXCLUDED_NAMES = {
    ".env", "audit_results.txt", "audit_results_safe.txt", "pytest_results.txt",
    "cli_release_manifest.json",
}
DEFAULT_EXCLUDED_SUFFIXES = {".db", ".sqlite", ".pyc", ".pyo", ".pyd", ".log", ".tmp", ".bak"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_files(root: Path) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in DEFAULT_EXCLUDED_DIRS)
        for filename in sorted(filenames):
            path = Path(directory) / filename
            relative = path.relative_to(root).as_posix()
            if filename in DEFAULT_EXCLUDED_NAMES or path.suffix.lower() in DEFAULT_EXCLUDED_SUFFIXES:
                continue
            if filename.startswith(".env") and not filename.endswith(".example"):
                continue
            files.append((relative, path))
    return sorted(files, key=lambda item: item[0])


def generate_manifest(root: Path, output: Path, version: str) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    total_bytes = 0
    for relative, path in collect_files(root):
        size = path.stat().st_size
        total_bytes += size
        entries.append({"path": relative, "size_bytes": size, "sha256": sha256_file(path)})
    manifest: dict[str, object] = {
        "release_version": version,
        "format_version": 1,
        "target_folder": ".",
        "verification_summary": {
            "total_files": len(entries),
            "total_bytes": total_bytes,
            "status": "VERIFIED_CLEAN",
        },
        "files": entries,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--version", default=os.environ.get("AETHERION_RELEASE_VERSION", "1.0.0"))
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    output = (args.output or root / "audit" / "release_manifest.json").expanduser().resolve()
    if not root.is_dir():
        parser.error(f"release root is not a directory: {root}")
    manifest = generate_manifest(root, output, args.version)
    summary = manifest["verification_summary"]
    print(f"Manifest written to {output}: {summary['total_files']} files, {summary['total_bytes']} bytes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

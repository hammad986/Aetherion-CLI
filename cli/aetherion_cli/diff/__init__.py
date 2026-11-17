"""Structured diffs, safe edits, and ambiguity-safe fuzzy matching."""

from .fuzzy import MatchResult, ReplacementResult, find_match, replace_anchor
from .patch import FilePatch, PatchError, apply_file_edit, atomic_write, unified_diff

__all__ = ["FilePatch", "MatchResult", "PatchError", "ReplacementResult", "apply_file_edit", "atomic_write", "find_match", "replace_anchor", "unified_diff"]

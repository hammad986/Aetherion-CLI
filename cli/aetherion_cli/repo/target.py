"""Query-targeted repository reading plans with a hard token ceiling."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .graph import RankedFile, rank_architecture

if TYPE_CHECKING:
    from .indexer import RepositoryIndex


MIN_PLAN_TOKENS = 2_000
MAX_PLAN_TOKENS = 4_000
_SECRET_PATTERN = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)\b\s*[:=]\s*)[^\s,;]+"
)


@dataclass(frozen=True, slots=True)
class ReadingPlanEntry:
    """One bounded file target in a reading plan."""

    path: str
    relevance: float
    reason: str
    symbols: tuple[str, ...] = ()
    excerpt: str = ""

    @property
    def estimated_tokens(self) -> int:
        return estimate_plan_tokens(self.excerpt) + estimate_plan_tokens(self.path) + 12


@dataclass(frozen=True, slots=True)
class ReadingPlan:
    """A rendered, deterministic plan that fits the requested input budget."""

    query: str
    budget_tokens: int
    entries: tuple[ReadingPlanEntry, ...]

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.entries)

    @property
    def selected_files(self) -> tuple[str, ...]:
        return self.files

    @property
    def estimated_tokens(self) -> int:
        return estimate_plan_tokens(self.render())

    @property
    def token_count(self) -> int:
        return self.estimated_tokens

    def render(self) -> str:
        # A query is user-controlled metadata.  Cap it before rendering so a
        # pathological prompt cannot defeat the hard plan budget after all
        # file excerpts have been fitted.
        rendered_query = _truncate(self.query, 512)
        lines = [
            "# Repository reading plan",
            f"Query: {rendered_query or '(architectural overview)'}",
            f"Budget: {self.budget_tokens} tokens maximum",
            "",
        ]
        if not self.entries:
            lines.append("No supported source files were indexed.")
        for number, entry in enumerate(self.entries, 1):
            lines.extend(
                [
                    f"## {number}. {entry.path}",
                    f"Relevance: {entry.relevance:.6f} | Reason: {entry.reason}",
                    f"Definitions: {', '.join(entry.symbols) if entry.symbols else '(none indexed)'}",
                    "",
                    entry.excerpt or "(file has no readable excerpt)",
                    "",
                ]
            )
        return "\n".join(lines).rstrip()

    def as_text(self) -> str:
        return self.render()

    def to_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "budget_tokens": self.budget_tokens,
            "estimated_tokens": self.estimated_tokens,
            "files": [
                {
                    "path": entry.path,
                    "relevance": entry.relevance,
                    "reason": entry.reason,
                    "symbols": list(entry.symbols),
                    "excerpt": entry.excerpt,
                }
                for entry in self.entries
            ],
            "text": self.render(),
        }


def estimate_plan_tokens(text: str) -> int:
    """Estimate tokens with the same conservative four-characters-per-token rule."""

    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def build_reading_plan(
    index: RepositoryIndex,
    query: str = "",
    token_budget: int = 3_000,
    *,
    budget_tokens: int | None = None,
    max_files: int = 10,
) -> ReadingPlan:
    """Select query-relevant architecture targets and render bounded excerpts.

    ``token_budget`` is intentionally constrained to 2,000–4,000 tokens.  The
    resulting plan may be smaller for a small repository, but its rendered form
    can never exceed the requested upper bound.
    """

    if budget_tokens is not None:
        token_budget = budget_tokens
    if not MIN_PLAN_TOKENS <= token_budget <= MAX_PLAN_TOKENS:
        raise ValueError(f"token budget must be between {MIN_PLAN_TOKENS} and {MAX_PLAN_TOKENS}")
    if not 5 <= max_files <= 15:
        raise ValueError("max_files must be between 5 and 15")

    ranked = rank_architecture(index, query=query)
    terms = _query_terms(query)
    candidates = sorted(
        ((_target_score(index, item, terms), item) for item in ranked),
        key=lambda pair: (-pair[0], pair[1].path),
    )
    available = len(candidates)
    if available == 0:
        return ReadingPlan(query, token_budget, ())

    target_count = min(max_files, available)
    if available >= 5:
        target_count = max(5, target_count)
    selected = candidates[:target_count]
    entries = tuple(_entry_for(index, item, score, terms) for score, item in selected)
    entries = _fit_entries(ReadingPlan(query, token_budget, entries), available)
    plan = ReadingPlan(query, token_budget, entries)
    if plan.estimated_tokens > token_budget:
        # This final guard is intentionally local and deterministic: long paths
        # or unusual Unicode can make a character estimate differ by a few units.
        plan = ReadingPlan(query, token_budget, _hard_fit(plan, available))
    return plan


create_reading_plan = build_reading_plan


def _target_score(index: RepositoryIndex, ranked: RankedFile, terms: set[str]) -> float:
    item = index.files[ranked.path]
    path_terms = _query_terms(ranked.path)
    symbol_terms = {
        token
        for symbol in item.symbols
        for token in _query_terms(symbol.name)
    }
    import_terms = {
        token
        for reference in item.imports
        for token in _query_terms(reference.module)
    }
    return ranked.score + len(terms & path_terms) * 0.25 + len(terms & symbol_terms) * 0.35 + len(terms & import_terms) * 0.1


def _entry_for(
    index: RepositoryIndex,
    ranked: RankedFile,
    score: float,
    terms: set[str],
) -> ReadingPlanEntry:
    item = index.files[ranked.path]
    symbols = tuple(
        f"{symbol.kind} {symbol.qualified_name or symbol.name} (line {symbol.line_start})"
        for symbol in item.symbols[:12]
    )
    matching = [term for term in terms if term in _query_terms(ranked.path) or any(term in _query_terms(symbol.name) for symbol in item.symbols)]
    reasons = list(ranked.reasons) or ["architectural-ranking"]
    if matching:
        reasons.insert(0, "query:" + ",".join(sorted(matching)))
    excerpt = _excerpt(index.read(ranked.path), terms, max_chars=1_400)
    return ReadingPlanEntry(ranked.path, score, ", ".join(_unique(reasons)), symbols, excerpt)


def _fit_entries(plan: ReadingPlan, available: int) -> tuple[ReadingPlanEntry, ...]:
    entries = list(plan.entries)
    minimum_entries = min(5, available) if available else 0
    while entries and ReadingPlan(plan.query, plan.budget_tokens, tuple(entries)).estimated_tokens > plan.budget_tokens:
        longest = max(range(len(entries)), key=lambda index: len(entries[index].excerpt))
        current = entries[longest]
        excess_chars = max(80, (ReadingPlan(plan.query, plan.budget_tokens, tuple(entries)).estimated_tokens - plan.budget_tokens) * 4)
        if len(current.excerpt) > 120:
            entries[longest] = replace(current, excerpt=_truncate(current.excerpt, max(80, len(current.excerpt) - excess_chars)))
            continue
        if len(entries) > minimum_entries:
            entries.pop()
            continue
        break
    return tuple(entries)


def _hard_fit(plan: ReadingPlan, available: int) -> tuple[ReadingPlanEntry, ...]:
    entries = list(plan.entries)
    minimum_entries = min(5, available) if available else 0
    while entries and ReadingPlan(plan.query, plan.budget_tokens, tuple(entries)).estimated_tokens > plan.budget_tokens:
        if len(entries) > minimum_entries:
            entries.pop()
            continue
        longest = max(range(len(entries)), key=lambda index: len(entries[index].excerpt))
        current = entries[longest]
        if current.excerpt:
            entries[longest] = replace(current, excerpt=_truncate(current.excerpt, max(0, len(current.excerpt) - 40)))
        else:
            entries.pop(longest)
    if ReadingPlan(plan.query, plan.budget_tokens, tuple(entries)).estimated_tokens > plan.budget_tokens:
        entries.clear()
    return tuple(entries)


def _excerpt(source: str, terms: set[str], *, max_chars: int) -> str:
    safe_source = _SECRET_PATTERN.sub(r"\1<redacted>", source)
    lines = safe_source.splitlines()
    if not lines:
        return ""
    lowered_terms = {term for term in terms if len(term) >= 2}
    matching_lines = [
        number
        for number, line in enumerate(lines)
        if not lowered_terms or any(term in line.lower() for term in lowered_terms)
    ]
    center = matching_lines[0] if matching_lines else 0
    start = max(0, center - 3)
    selected: list[str] = []
    size = 0
    for number in range(start, len(lines)):
        candidate = f"{number + 1:>5}: {lines[number]}"
        if selected and size + len(candidate) + 1 > max_chars:
            break
        selected.append(candidate)
        size += len(candidate) + 1
    excerpt = "\n".join(selected)
    return _truncate(excerpt, max_chars)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return text[:max_chars]
    return text[: max_chars - 1].rstrip() + "…"


def _query_terms(value: str) -> set[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return {term for term in re.split(r"[^a-z0-9]+", expanded.lower()) if term}


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


__all__ = [
    "MAX_PLAN_TOKENS",
    "MIN_PLAN_TOKENS",
    "ReadingPlan",
    "ReadingPlanEntry",
    "build_reading_plan",
    "create_reading_plan",
    "estimate_plan_tokens",
]

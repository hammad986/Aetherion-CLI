"""Deterministic file dependency graphs and architecture ranking."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .indexer import FileIndex, ImportReference, RepositoryIndex


@dataclass(frozen=True, slots=True)
class DependencyGraph:
    """A directed graph where an edge points from importer to dependency."""

    nodes: tuple[str, ...]
    edges: Mapping[str, tuple[str, ...]]
    reverse_edges: Mapping[str, tuple[str, ...]]

    @property
    def adjacency(self) -> Mapping[str, tuple[str, ...]]:
        return self.edges

    @property
    def reverse(self) -> Mapping[str, tuple[str, ...]]:
        return self.reverse_edges

    @property
    def edge_count(self) -> int:
        return sum(len(targets) for targets in self.edges.values())

    def dependencies_of(self, path: str) -> tuple[str, ...]:
        return self.edges.get(path, ())

    def dependents_of(self, path: str) -> tuple[str, ...]:
        return self.reverse_edges.get(path, ())

    def to_dict(self) -> dict[str, object]:
        return {
            "nodes": list(self.nodes),
            "edges": {path: list(targets) for path, targets in self.edges.items()},
            "reverse_edges": {path: list(targets) for path, targets in self.reverse_edges.items()},
        }


@dataclass(frozen=True, slots=True)
class RankedFile:
    """Architecture-ranking evidence for one repository file."""

    path: str
    score: float
    pagerank: float
    in_degree: int
    out_degree: int
    symbol_count: int
    reasons: tuple[str, ...] = ()


def build_dependency_graph(index: RepositoryIndex | Mapping[str, object]) -> DependencyGraph:
    """Build a graph from indexed local import resolutions.

    Unresolved third-party imports are intentionally omitted.  A graph edge is
    only created when the target is another indexed file in the same repository.
    """

    if isinstance(index, RepositoryIndex):
        files: Mapping[str, object] = index.files
    else:
        files = index

    node_names = set(files)
    edge_sets: dict[str, set[str]] = {path: set() for path in node_names}
    for path, value in files.items():
        if isinstance(value, FileIndex):
            references: Iterable[object] = value.imports
        elif isinstance(value, Mapping):
            raw_imports = value.get("imports", ())
            references = raw_imports if isinstance(raw_imports, Iterable) else ()
        elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            references = value
        else:
            references = ()
        for reference in references:
            target: object | None
            if isinstance(reference, ImportReference):
                target = reference.resolved_path
            elif isinstance(reference, Mapping):
                target = reference.get("resolved_path", reference.get("target"))
            else:
                target = reference if isinstance(reference, str) else None
            if isinstance(target, str) and target in node_names and target != path:
                edge_sets[path].add(target)

    edges = {path: tuple(sorted(edge_sets[path])) for path in sorted(node_names)}
    reverse_sets: dict[str, set[str]] = {path: set() for path in node_names}
    for source, targets in edges.items():
        for target in targets:
            reverse_sets[target].add(source)
    reverse_edges = {path: tuple(sorted(reverse_sets[path])) for path in sorted(node_names)}
    return DependencyGraph(tuple(sorted(node_names)), edges, reverse_edges)


def _as_graph(graph: DependencyGraph | Mapping[str, Iterable[str]]) -> DependencyGraph:
    if isinstance(graph, DependencyGraph):
        return graph
    node_names = set(graph)
    for targets in graph.values():
        node_names.update(targets)
    edges = {path: tuple(sorted(set(graph.get(path, ())))) for path in sorted(node_names)}
    reverse_sets: dict[str, set[str]] = {path: set() for path in node_names}
    for source, targets in edges.items():
        for target in targets:
            reverse_sets[target].add(source)
    reverse_edges = {path: tuple(sorted(reverse_sets[path])) for path in sorted(node_names)}
    return DependencyGraph(tuple(sorted(node_names)), edges, reverse_edges)


def page_rank(
    graph: DependencyGraph | Mapping[str, Iterable[str]],
    *,
    damping: float = 0.85,
    max_iterations: int = 100,
    tolerance: float = 1e-12,
) -> dict[str, float]:
    """Compute deterministic PageRank without NetworkX or other dependencies.

    Edges point from a file to its imports, so imported/shared files receive
    authority from their dependents.  Sorted node and edge iteration makes the
    result stable across Python processes and mapping implementations.
    """

    if not 0.0 <= damping < 1.0:
        raise ValueError("damping must be in the range [0, 1)")
    if max_iterations < 1:
        raise ValueError("max_iterations must be positive")
    if tolerance <= 0:
        raise ValueError("tolerance must be positive")
    normalized = _as_graph(graph)
    nodes = normalized.nodes
    if not nodes:
        return {}
    count = len(nodes)
    initial = 1.0 / count
    scores = {node: initial for node in nodes}
    for _ in range(max_iterations):
        dangling_mass = sum(
            scores[node] for node in nodes if not normalized.edges.get(node, ())
        )
        updated: dict[str, float] = {}
        for node in nodes:
            incoming = sum(
                scores[source] / len(normalized.edges[source])
                for source in normalized.reverse_edges.get(node, ())
                if normalized.edges.get(source)
            )
            updated[node] = (1.0 - damping) / count + damping * (dangling_mass / count + incoming)
        delta = sum(abs(updated[node] - scores[node]) for node in nodes)
        scores = updated
        if delta <= tolerance:
            break
    total = sum(scores.values())
    if total == 0.0:
        return {node: initial for node in nodes}
    return {node: scores[node] / total for node in nodes}


pagerank = page_rank


def rank_architecture(
    index: RepositoryIndex,
    graph: DependencyGraph | None = None,
    *,
    query: str = "",
) -> tuple[RankedFile, ...]:
    """Rank files using PageRank plus explainable structural tie-breakers."""

    dependency_graph = graph or build_dependency_graph(index)
    scores = page_rank(dependency_graph)
    query_terms = _query_terms(query)
    ranked: list[RankedFile] = []
    for path in dependency_graph.nodes:
        item = index.files.get(path)
        if item is None:
            continue
        in_degree = len(dependency_graph.reverse_edges.get(path, ()))
        out_degree = len(dependency_graph.edges.get(path, ()))
        symbol_count = len(item.symbols)
        path_terms = set(re.split(r"[^a-z0-9]+", path.lower()))
        symbol_terms = {
            piece
            for symbol in item.symbols
            for piece in re.split(r"[^a-z0-9]+", symbol.name.lower())
            if piece
        }
        query_hits = len(query_terms & (path_terms | symbol_terms))
        entrypoint = 1 if _is_common_entrypoint(path) else 0
        score = (
            scores.get(path, 0.0)
            + in_degree * 0.01
            + min(out_degree, 20) * 0.0002
            + min(symbol_count, 100) * 0.0001
            + query_hits * 0.03
            + entrypoint * 0.001
        )
        reasons: list[str] = []
        if scores.get(path, 0.0) > 0:
            reasons.append("dependency-centrality")
        if in_degree:
            reasons.append(f"{in_degree}-dependent-file" if in_degree == 1 else f"{in_degree}-dependent-files")
        if symbol_count:
            reasons.append("symbol-rich")
        if query_hits:
            reasons.append("query-match")
        if entrypoint:
            reasons.append("common-entrypoint")
        ranked.append(RankedFile(path, score, scores.get(path, 0.0), in_degree, out_degree, symbol_count, tuple(reasons)))
    return tuple(sorted(ranked, key=lambda item: (-item.score, -item.pagerank, item.path)))


def rank_critical_files(
    index: RepositoryIndex,
    graph: DependencyGraph | None = None,
    *,
    query: str = "",
) -> tuple[RankedFile, ...]:
    """Descriptive alias for :func:`rank_architecture`."""

    return rank_architecture(index, graph, query=query)


def select_critical_files(
    index: RepositoryIndex,
    top_n: int = 10,
    *,
    min_files: int = 5,
    max_files: int = 15,
    query: str = "",
) -> tuple[str, ...]:
    """Select a deterministic top-five-to-fifteen architectural file set."""

    if min_files < 1 or max_files < min_files:
        raise ValueError("file bounds must satisfy 1 <= min_files <= max_files")
    if top_n < min_files or top_n > max_files:
        raise ValueError(f"top_n must be between {min_files} and {max_files}")
    ranked = rank_architecture(index, query=query)
    count = min(len(ranked), top_n)
    if count == 0:
        return ()
    return tuple(item.path for item in ranked[:count])


def critical_files(
    index: RepositoryIndex,
    top_n: int = 10,
    *,
    min_files: int = 5,
    max_files: int = 15,
    query: str = "",
    limit: int | None = None,
) -> tuple[str, ...]:
    """Return the paths of the top architecturally critical files."""

    if limit is not None:
        top_n = limit
    return select_critical_files(index, top_n, min_files=min_files, max_files=max_files, query=query)


def _query_terms(query: str) -> set[str]:
    return {term for term in re.split(r"[^a-z0-9]+", query.lower()) if term}


def _is_common_entrypoint(path: str) -> bool:
    """Recognize common application entrypoints without importing the file."""

    name = path.rsplit("/", 1)[-1].lower()
    return name in {"main.py", "__main__.py", "app.py", "server.py", "index.js", "index.ts", "main.go", "main.rs"}


__all__ = [
    "DependencyGraph",
    "RankedFile",
    "build_dependency_graph",
    "critical_files",
    "page_rank",
    "pagerank",
    "rank_architecture",
    "rank_critical_files",
    "select_critical_files",
]

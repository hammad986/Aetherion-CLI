"""Repository indexing, dependency analysis, and bounded target selection."""

from .graph import (
    DependencyGraph,
    RankedFile,
    build_dependency_graph,
    critical_files,
    page_rank,
    pagerank,
    rank_architecture,
    rank_critical_files,
    select_critical_files,
)
from .indexer import (
    FileIndex,
    ImportReference,
    IndexedFile,
    RepositoryIndex,
    RepositoryIndexer,
    Symbol,
    SymbolDefinition,
    build_index,
    index_repository,
)
from .target import (
    ReadingPlan,
    ReadingPlanEntry,
    build_reading_plan,
    create_reading_plan,
    estimate_plan_tokens,
)

__all__ = [
    "DependencyGraph",
    "FileIndex",
    "ImportReference",
    "IndexedFile",
    "RankedFile",
    "ReadingPlan",
    "ReadingPlanEntry",
    "RepositoryIndex",
    "RepositoryIndexer",
    "Symbol",
    "SymbolDefinition",
    "build_dependency_graph",
    "build_index",
    "build_reading_plan",
    "create_reading_plan",
    "critical_files",
    "estimate_plan_tokens",
    "index_repository",
    "page_rank",
    "pagerank",
    "rank_architecture",
    "rank_critical_files",
    "select_critical_files",
]

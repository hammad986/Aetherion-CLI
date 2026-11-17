"""Focused Phase 6 repository intelligence tests."""

from __future__ import annotations

from pathlib import Path

from aetherion_cli.repo import (
    build_dependency_graph,
    build_reading_plan,
    critical_files,
    index_repository,
    page_rank,
)


def _write_fixture(root: Path) -> None:
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("from .core import Service\n", encoding="utf-8")
    (root / "pkg" / "core.py").write_text(
        "from .util import helper\n\nclass Service:\n    def run(self) -> str:\n        return helper()\n",
        encoding="utf-8",
    )
    (root / "pkg" / "util.py").write_text(
        "def helper() -> str:\n    return 'ok'\n",
        encoding="utf-8",
    )
    (root / "ui.ts").write_text(
        "import { Service } from './pkg/core';\nexport class Panel {\n  render(): Service { return new Service(); }\n}\n",
        encoding="utf-8",
    )
    (root / "go.mod").write_text("module example.com/demo\n\ngo 1.22\n", encoding="utf-8")
    (root / "util").mkdir()
    (root / "util" / "helper.go").write_text(
        "package util\n\nfunc Help() {}\n",
        encoding="utf-8",
    )
    (root / "worker.go").write_text(
        "package worker\n\nimport \"example.com/demo/util\"\n\ntype Runner struct{}\nfunc (r Runner) Run() { util.Help() }\n",
        encoding="utf-8",
    )
    (root / "helper.rs").write_text("pub fn run() {}\n", encoding="utf-8")
    (root / "lib.rs").write_text(
        "pub struct Engine;\npub fn start() {}\nuse crate::helper::run;\n",
        encoding="utf-8",
    )


def test_index_extracts_symbols_and_local_imports_without_optional_grammars(tmp_path: Path) -> None:
    _write_fixture(tmp_path)

    index = index_repository(tmp_path)

    assert {symbol.name for symbol in index.files["pkg/core.py"].symbols} >= {"Service", "run"}
    assert any(symbol.name == "run" and symbol.kind == "method" for symbol in index.files["pkg/core.py"].symbols)
    assert index.files["pkg/core.py"].imports[0].resolved_path == "pkg/util.py"
    assert {symbol.name for symbol in index.files["ui.ts"].symbols} >= {"Panel"}
    assert {symbol.name for symbol in index.files["worker.go"].symbols} >= {"Runner", "Run"}
    assert {symbol.name for symbol in index.files["lib.rs"].symbols} >= {"Engine", "start"}
    assert index.files["worker.go"].imports[0].resolved_path == "util/helper.go"
    assert index.files["lib.rs"].imports[0].resolved_path == "helper.rs"


def test_dependency_graph_and_pagerank_are_deterministic(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    index = index_repository(tmp_path)

    graph = build_dependency_graph(index)
    first = page_rank(graph)
    second = page_rank(graph)

    assert first == second
    assert "pkg/util.py" in graph.dependencies_of("pkg/core.py")
    assert "pkg/core.py" in graph.dependents_of("pkg/util.py")
    assert "util/helper.go" in graph.dependencies_of("worker.go")
    assert "helper.rs" in graph.dependencies_of("lib.rs")
    assert critical_files(index, top_n=5) == critical_files(index, top_n=5)
    assert len(critical_files(index, top_n=5)) == 5


def test_query_reading_plan_is_bounded_and_redacts_secret_literals(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    (tmp_path / "pkg" / "core.py").write_text(
        "from .util import helper\nAPI_KEY = 'local-secret-value'\n\nclass Service:\n    def run(self):\n        return helper()\n",
        encoding="utf-8",
    )
    index = index_repository(tmp_path)

    plan = build_reading_plan(index, "Service run", budget_tokens=2_000, max_files=5)

    assert 1 <= len(plan.entries) <= 5
    assert plan.estimated_tokens <= 2_000
    assert "Service" in plan.render()
    assert "local-secret-value" not in plan.render()
    assert "<redacted>" in plan.render()

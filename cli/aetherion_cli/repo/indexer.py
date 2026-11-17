"""Bounded, local repository indexing with optional Tree-sitter extraction.

The indexer deliberately has no package-install or network behavior.  Tree-sitter
is discovered at runtime when it is already installed; Python's standard AST and
small language-aware lexical fallbacks keep the index useful in a minimal CLI
installation.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import os
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
}

_SKIPPED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
    "vendor",
    "target",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}
_IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$]*"


@dataclass(frozen=True, slots=True)
class Symbol:
    """A definition extracted from one source file."""

    path: str
    name: str
    kind: str
    line_start: int
    line_end: int
    column_start: int = 0
    column_end: int = 0
    qualified_name: str = ""
    parent: str | None = None
    signature: str = ""
    language: str = ""

    @property
    def symbol_type(self) -> str:
        """Compatibility spelling for consumers that call the kind a type."""

        return self.kind

    @property
    def line(self) -> int:
        """Return the one-based definition start line."""

        return self.line_start


SymbolDefinition = Symbol


@dataclass(frozen=True, slots=True)
class ImportReference:
    """An import statement and its best-effort local resolution."""

    path: str
    module: str
    names: tuple[str, ...] = ()
    line: int = 1
    level: int = 0
    resolved_path: str | None = None

    @property
    def target(self) -> str | None:
        """Alias for the resolved repository-relative target."""

        return self.resolved_path


@dataclass(frozen=True, slots=True)
class FileIndex:
    """The local facts collected for one supported source file."""

    path: str
    language: str
    size_bytes: int
    line_count: int
    symbols: tuple[Symbol, ...] = ()
    imports: tuple[ImportReference, ...] = ()
    parser: str = "fallback"
    content_sha256: str = ""

    @property
    def definitions(self) -> tuple[Symbol, ...]:
        return self.symbols

    @property
    def classes(self) -> tuple[Symbol, ...]:
        return tuple(symbol for symbol in self.symbols if symbol.kind in {"class", "interface", "struct", "enum", "trait"})

    @property
    def functions(self) -> tuple[Symbol, ...]:
        return tuple(symbol for symbol in self.symbols if symbol.kind in {"function", "method"})

    @property
    def import_modules(self) -> tuple[str, ...]:
        return tuple(reference.module for reference in self.imports)


IndexedFile = FileIndex


@dataclass(frozen=True, slots=True)
class RepositoryIndex:
    """A deterministic snapshot of a repository's supported source files."""

    root: Path
    files: Mapping[str, FileIndex]

    @property
    def file_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self.files))

    @property
    def symbols(self) -> tuple[Symbol, ...]:
        return tuple(symbol for path in self.file_paths for symbol in self.files[path].symbols)

    @property
    def imports(self) -> tuple[ImportReference, ...]:
        return tuple(reference for path in self.file_paths for reference in self.files[path].imports)

    @property
    def symbol_count(self) -> int:
        return len(self.symbols)

    def file(self, path: str | Path) -> FileIndex | None:
        """Return a file by repository-relative path."""

        return self.files.get(_as_relative_path(path))

    def find_symbol(self, name: str) -> tuple[Symbol, ...]:
        """Find exact or qualified-name matches in deterministic order."""

        return tuple(
            symbol
            for symbol in self.symbols
            if symbol.name == name or symbol.qualified_name == name
        )

    def read(self, path: str | Path, *, max_bytes: int = 1_000_000) -> str:
        """Read an indexed file without allowing access outside the root."""

        relative = _as_relative_path(path)
        if relative not in self.files:
            raise KeyError(f"file is not in the repository index: {relative}")
        candidate = (self.root / Path(relative)).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("file is outside the repository root") from exc
        return candidate.read_bytes()[:max_bytes].decode("utf-8", errors="replace")

    def to_dict(self) -> dict[str, object]:
        """Return a stable, JSON-friendly representation for diagnostics."""

        return {
            "root": str(self.root),
            "files": {
                path: {
                    "language": item.language,
                    "size_bytes": item.size_bytes,
                    "line_count": item.line_count,
                    "parser": item.parser,
                    "symbols": [
                        {
                            "name": symbol.name,
                            "kind": symbol.kind,
                            "qualified_name": symbol.qualified_name,
                            "line_start": symbol.line_start,
                            "line_end": symbol.line_end,
                        }
                        for symbol in item.symbols
                    ],
                    "imports": [
                        {
                            "module": reference.module,
                            "names": list(reference.names),
                            "line": reference.line,
                            "resolved_path": reference.resolved_path,
                        }
                        for reference in item.imports
                    ],
                }
                for path, item in sorted(self.files.items())
            },
        }


class RepositoryIndexer:
    """Build a source-only index using deterministic filesystem traversal."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_files: int = 2_000,
        max_file_bytes: int = 1_000_000,
    ) -> None:
        if max_files < 1:
            raise ValueError("max_files must be positive")
        if max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive")
        self.root = Path(root).expanduser().resolve()
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes

    def build(self) -> RepositoryIndex:
        """Index supported files, returning an empty index for an empty tree."""

        if not self.root.exists() or not self.root.is_dir():
            raise ValueError(f"repository root is not a directory: {self.root}")

        discovered = list(self._iter_source_paths())[: self.max_files]
        provisional: dict[str, FileIndex] = {}
        for path in discovered:
            relative = path.relative_to(self.root).as_posix()
            try:
                raw = path.read_bytes()
            except (OSError, UnicodeError):
                continue
            if len(raw) > self.max_file_bytes:
                continue
            text = raw.decode("utf-8", errors="replace")
            item = _index_source(relative, text, len(raw), self.max_file_bytes)
            provisional[relative] = item

        module_map = _build_module_map(provisional)
        files = {
            path: replace(
                item,
                imports=tuple(
                    replace(
                        reference,
                        resolved_path=_resolve_import(
                            self.root,
                            item,
                            reference,
                            provisional,
                            module_map,
                        ),
                    )
                    for reference in item.imports
                ),
            )
            for path, item in sorted(provisional.items())
        }
        return RepositoryIndex(self.root, files)

    index = build

    def _iter_source_paths(self) -> Iterator[Path]:
        for directory, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = sorted(
                name for name in dirnames if name not in _SKIPPED_DIRECTORIES and not name.startswith(".")
            )
            for filename in sorted(filenames):
                path = Path(directory) / filename
                if (
                    path.suffix.lower() in LANGUAGE_BY_SUFFIX
                    and not filename.startswith(".")
                    and not path.is_symlink()
                ):
                    yield path


def index_repository(root: str | Path, **kwargs: int) -> RepositoryIndex:
    """Convenience function for building a repository index."""

    return RepositoryIndexer(root, **kwargs).build()


build_index = index_repository


def _as_relative_path(path: str | Path) -> str:
    value = Path(path)
    if value.is_absolute():
        raise ValueError("repository paths must be relative")
    parts = value.as_posix().split("/")
    if ".." in parts:
        raise ValueError("repository paths may not traverse the repository root")
    return value.as_posix().removeprefix("./")


def _language_for(path: str) -> str:
    return LANGUAGE_BY_SUFFIX.get(Path(path).suffix.lower(), "unknown")


def _index_source(path: str, source: str, size: int, max_file_bytes: int) -> FileIndex:
    language = _language_for(path)
    del max_file_bytes  # The caller performs the size gate; this keeps the parser API explicit.
    parsed = _tree_sitter_extract(path, source, language)
    fallback_symbols: list[Symbol]
    fallback_imports: list[ImportReference]
    if language == "python":
        fallback_symbols, fallback_imports = _python_fallback(path, source)
    else:
        fallback_symbols, fallback_imports = _regex_fallback(path, source, language)
    if parsed is not None:
        # Tree-sitter grammars vary by installed version.  Merge the
        # deterministic fallback facts rather than treating a partial parse
        # (for example imports without arrow-function definitions) as complete.
        parsed_symbols, parsed_imports = parsed
        symbols = _unique_symbols([*parsed_symbols, *fallback_symbols])
        imports = _unique_imports([*parsed_imports, *fallback_imports])
        parser = "tree-sitter"
    elif language == "python":
        symbols, imports = fallback_symbols, fallback_imports
        parser = "python-ast" if _python_parses(source) else "regex"
    else:
        symbols, imports = fallback_symbols, fallback_imports
        parser = "regex"

    if not imports:
        imports = _fallback_imports(path, source, language)
    return FileIndex(
        path=path,
        language=language,
        size_bytes=size,
        line_count=max(1, source.count("\n") + 1),
        symbols=tuple(sorted(symbols, key=lambda symbol: (symbol.line_start, symbol.column_start, symbol.name, symbol.kind))),
        imports=tuple(sorted(_unique_imports(imports), key=lambda reference: (reference.line, reference.module, reference.names))),
        parser=parser,
        content_sha256=hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest(),
    )


def _python_parses(source: str) -> bool:
    try:
        ast.parse(source)
    except SyntaxError:
        return False
    return True


@lru_cache(maxsize=8)
def _tree_sitter_parser(language: str) -> Any | None:
    """Return a parser when an installed Tree-sitter grammar can be loaded."""

    try:
        tree_sitter_languages = importlib.import_module("tree_sitter_languages")
        get_parser = getattr(tree_sitter_languages, "get_parser", None)
        if get_parser is not None:
            return get_parser(language)
    except (ImportError, AttributeError, LookupError, OSError, RuntimeError, TypeError, ValueError):
        pass

    try:
        tree_sitter = importlib.import_module("tree_sitter")
        parser_type = tree_sitter.Parser
        grammar_module_name = "tree_sitter_typescript" if language == "tsx" else f"tree_sitter_{language}"
        grammar_module = importlib.import_module(grammar_module_name)
        factory_name = {
            "typescript": "language_typescript",
            "tsx": "language_tsx",
        }.get(language, "language")
        grammar_factory = getattr(grammar_module, factory_name, None) or grammar_module.language
        grammar = grammar_factory()
        try:
            grammar = tree_sitter.Language(grammar)
        except (TypeError, ValueError):
            pass
        parser = parser_type()
        if hasattr(parser, "language"):
            parser.language = grammar
        else:
            parser.set_language(grammar)
        return parser
    except (ImportError, AttributeError, LookupError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _tree_sitter_extract(path: str, source: str, language: str) -> tuple[list[Symbol], list[ImportReference]] | None:
    if language == "unknown":
        return None
    parser = _tree_sitter_parser(language)
    if parser is None:
        return None
    try:
        tree = parser.parse(source.encode("utf-8"))
        root = tree.root_node
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None

    symbols: list[Symbol] = []
    imports: list[ImportReference] = []
    definition_types = {
        "class_definition": "class",
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "type_alias_declaration": "type",
        "struct_item": "struct",
        "enum_item": "enum",
        "trait_item": "trait",
        "type_spec": "type",
        "function_definition": "function",
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "function_item": "function",
        "method_definition": "method",
        "method_declaration": "method",
        "impl_item": "class",
    }
    import_types = {
        "import_statement",
        "import_from_statement",
        "import_declaration",
        "import_spec",
        "use_declaration",
        "use_clause",
        "use_list",
        "extern_crate_declaration",
    }

    def visit(node: Any, containers: tuple[str, ...], container_kinds: tuple[str, ...]) -> None:
        node_type = str(getattr(node, "type", ""))
        text = _tree_node_text(node, source)
        next_containers = containers
        next_container_kinds = container_kinds
        kind = definition_types.get(node_type)
        if kind is not None and node_type != "impl_item":
            name = _tree_name(node, text, language)
            if name:
                actual_kind = (
                    "method"
                    if kind == "function"
                    and container_kinds
                    and container_kinds[-1] in {"class", "interface", "impl"}
                    else kind
                )
                qualified = ".".join((*containers, name))
                symbols.append(
                    _tree_symbol(path, language, node, name, actual_kind, qualified, containers[-1] if containers else None, text)
                )
                if kind in {"class", "interface", "struct", "enum", "trait"}:
                    next_containers = (*containers, name)
                    next_container_kinds = (*container_kinds, kind)
        elif node_type == "impl_item":
            impl_name = _tree_impl_name(text, language)
            if impl_name:
                next_containers = (*containers, impl_name)
                next_container_kinds = (*container_kinds, "impl")

        if node_type in import_types:
            imports.extend(_imports_from_line(path, text, language, _tree_line(node)))
        for child in getattr(node, "children", ()):
            visit(child, next_containers, next_container_kinds)

    visit(root, (), ())
    if not symbols and not imports:
        return None
    return symbols, imports


def _tree_node_text(node: Any, source: str) -> str:
    start = int(getattr(node, "start_byte", 0))
    end = int(getattr(node, "end_byte", start))
    encoded = source.encode("utf-8")
    return encoded[start:end].decode("utf-8", errors="replace")


def _tree_line(node: Any) -> int:
    point = getattr(node, "start_point", (0, 0))
    return int(point[0]) + 1 if isinstance(point, tuple) else int(getattr(point, "row", 0)) + 1


def _tree_end(node: Any) -> tuple[int, int]:
    point = getattr(node, "end_point", (0, 0))
    if isinstance(point, tuple):
        return int(point[0]) + 1, int(point[1])
    return int(getattr(point, "row", 0)) + 1, int(getattr(point, "column", 0))


def _tree_name(node: Any, text: str, language: str) -> str:
    for field in ("name", "declarator", "type"):
        try:
            child = node.child_by_field_name(field)
        except (AttributeError, TypeError):
            child = None
        if child is not None:
            candidate = getattr(child, "text", b"")
            if isinstance(candidate, bytes):
                candidate = candidate.decode("utf-8", errors="replace")
            if isinstance(candidate, str) and re.fullmatch(_IDENTIFIER, candidate):
                return candidate
    keyword_pattern = r"(?:class|interface|struct|enum|trait|fn|func|function|type|mod)\s+(_?\w+)"
    match = re.search(keyword_pattern, text)
    if match:
        return match.group(1)
    if language in {"javascript", "typescript", "tsx"}:
        match = re.search(rf"(?:const|let|var)\s+({_IDENTIFIER})\s*=", text)
        if match:
            return match.group(1)
    return ""


def _tree_impl_name(text: str, language: str) -> str:
    if language == "rust":
        match = re.search(r"impl(?:<[^>]+>)?\s+(?:[^\s{]+\s+for\s+)?(" + _IDENTIFIER + r")", text)
        return match.group(1) if match else "impl"
    return "impl"


def _tree_symbol(
    path: str,
    language: str,
    node: Any,
    name: str,
    kind: str,
    qualified: str,
    parent: str | None,
    text: str,
) -> Symbol:
    end_line, end_column = _tree_end(node)
    point = getattr(node, "start_point", (0, 0))
    start_column = int(point[1]) if isinstance(point, tuple) else int(getattr(point, "column", 0))
    signature = text.splitlines()[0].strip() if text else name
    return Symbol(path, name, kind, _tree_line(node), max(_tree_line(node), end_line), start_column, end_column, qualified, parent, signature, language)


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self, path: str, source: str) -> None:
        self.path = path
        self.source = source
        self.symbols: list[Symbol] = []
        self.imports: list[ImportReference] = []
        self.containers: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add_definition(node, "class")
        self.containers.append(node.name)
        self.generic_visit(node)
        self.containers.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._add_definition(node, "method" if self.containers else "function")
        self.containers.append(node.name)
        self.generic_visit(node)
        self.containers.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._add_definition(node, "method" if self.containers else "function")
        self.containers.append(node.name)
        self.generic_visit(node)
        self.containers.pop()

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.extend(
            ImportReference(self.path, alias.name, (alias.asname or alias.name.split(".")[-1],), node.lineno)
            for alias in node.names
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        self.imports.append(
            ImportReference(self.path, module, tuple(alias.name for alias in node.names), node.lineno, node.level)
        )

    def _add_definition(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, kind: str) -> None:
        start = min((decorator.lineno for decorator in node.decorator_list), default=node.lineno)
        end = getattr(node, "end_lineno", node.lineno)
        qualified = ".".join((*self.containers, node.name))
        lines = self.source.splitlines()
        signature = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else node.name
        self.symbols.append(
            Symbol(self.path, node.name, kind, start, end, node.col_offset, 0, qualified, self.containers[-1] if self.containers else None, signature, "python")
        )


def _python_fallback(path: str, source: str) -> tuple[list[Symbol], list[ImportReference]]:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return _regex_fallback(path, source, "python")
    visitor = _PythonVisitor(path, source)
    visitor.visit(tree)
    return visitor.symbols, visitor.imports


def _regex_fallback(path: str, source: str, language: str) -> tuple[list[Symbol], list[ImportReference]]:
    symbols: list[Symbol] = []
    containers: list[tuple[str, int, str]] = []
    brace_depth = 0
    lines = source.splitlines()
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        kind_name: tuple[str, str] | None = None
        patterns: tuple[tuple[str, str], ...]
        if language == "python":
            patterns = ((r"^\s*class\s+(" + _IDENTIFIER + r")", "class"), (r"^\s*(?:async\s+)?def\s+(" + _IDENTIFIER + r")", "function"))
        elif language in {"javascript", "typescript", "tsx"}:
            patterns = (
                (r"^\s*(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+(" + _IDENTIFIER + r")", "class"),
                (r"^\s*(?:export\s+)?interface\s+(" + _IDENTIFIER + r")", "interface"),
                (r"^\s*(?:export\s+)?(?:async\s+)?function\s+(" + _IDENTIFIER + r")", "function"),
                (r"^\s*(?:export\s+)?(?:const|let|var)\s+(" + _IDENTIFIER + r")\s*=\s*(?:async\s*)?\(", "function"),
                (r"^\s*(?:async\s+)?(" + _IDENTIFIER + r")\s*\([^)]*\)\s*[:{]", "method"),
            )
        elif language == "go":
            patterns = (
                (r"^\s*type\s+(" + _IDENTIFIER + r")\s+struct\b", "struct"),
                (r"^\s*type\s+(" + _IDENTIFIER + r")\s+interface\b", "interface"),
                (r"^\s*func\s+\((?:[^)]*)\)\s+(" + _IDENTIFIER + r")\s*\(", "method"),
                (r"^\s*func\s+(" + _IDENTIFIER + r")\s*\(", "function"),
            )
        else:
            patterns = (
                (r"^\s*(?:pub\s+)?struct\s+(" + _IDENTIFIER + r")", "struct"),
                (r"^\s*(?:pub\s+)?enum\s+(" + _IDENTIFIER + r")", "enum"),
                (r"^\s*(?:pub\s+)?trait\s+(" + _IDENTIFIER + r")", "trait"),
                (r"^\s*impl(?:<[^>]+>)?\s+(?:[^\s{]+\s+for\s+)?(" + _IDENTIFIER + r")", "class"),
                (r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(" + _IDENTIFIER + r")", "function"),
                (r"^\s*(?:pub\s+)?mod\s+(" + _IDENTIFIER + r")", "class"),
                (r"^\s*(?:pub\s+)?type\s+(" + _IDENTIFIER + r")", "type"),
                (r"^\s*(?:pub\s+)?(?:const|static)\s+(" + _IDENTIFIER + r")", "constant"),
            )
        for pattern, kind in patterns:
            match = re.search(pattern, line)
            if match:
                kind_name = (kind, match.group(1))
                break
        if kind_name is not None:
            kind, name = kind_name
            parent = containers[-1][0] if containers else None
            if kind == "function" and parent and language in {"javascript", "typescript", "tsx", "rust"}:
                kind = "method"
            qualified = ".".join((*[item[0] for item in containers], name))
            symbols.append(Symbol(path, name, kind, number, number, len(line) - len(line.lstrip()), 0, qualified, parent, stripped, language))
            if kind in {"class", "interface", "struct", "enum", "trait"}:
                containers.append((name, brace_depth, kind))
        brace_depth += line.count("{") - line.count("}")
        while containers and brace_depth <= containers[-1][1]:
            containers.pop()
    return symbols, _fallback_imports(path, source, language)


def _fallback_imports(path: str, source: str, language: str) -> list[ImportReference]:
    return [reference for number, line in enumerate(source.splitlines(), 1) for reference in _imports_from_line(path, line, language, number)]


def _imports_from_line(path: str, line: str, language: str, number: int) -> list[ImportReference]:
    stripped = line.strip()
    references: list[ImportReference] = []
    if language == "python":
        match = re.match(r"import\s+(.+)$", stripped)
        if match:
            for item in match.group(1).split(","):
                module = item.strip().split(" as ", 1)[0].strip()
                if module:
                    references.append(ImportReference(path, module, (module.split(".")[-1],), number))
        match = re.match(r"from\s+([.\w]*)\s+import\s+(.+)$", stripped)
        if match:
            raw_module, raw_names = match.groups()
            level = len(raw_module) - len(raw_module.lstrip("."))
            module = raw_module[level:]
            names = tuple(item.strip().split(" as ", 1)[0] for item in raw_names.strip("() ").split(",") if item.strip())
            references.append(ImportReference(path, module, names, number, level))
    elif language in {"javascript", "typescript", "tsx"}:
        matches = re.findall(r"(?:from\s*|import\s*\(|require\s*\()([\"'])([^\"']+)\1", stripped)
        side_effect = re.match(r"import\s*([\"'])([^\"']+)\1", stripped)
        if side_effect:
            matches.append(side_effect.groups())
        for _, module in matches:
            references.append(ImportReference(path, module, (), number, 1 if module.startswith(".") else 0))
    elif language == "go":
        match = re.search(r'"([^"\n]+)"', stripped)
        if match and (stripped.startswith("import") or stripped.startswith('"')):
            references.append(ImportReference(path, match.group(1), (), number))
    elif language == "rust":
        match = re.match(r"(?:pub\s+)?use\s+([^;]+)", stripped)
        if match:
            module = match.group(1).split("::{", 1)[0].strip()
            references.append(ImportReference(path, module, (), number, 1 if module.startswith(("self", "super")) else 0))
        match = re.match(r"(?:pub\s+)?mod\s+(" + _IDENTIFIER + r")", stripped)
        if match:
            references.append(ImportReference(path, match.group(1), (), number))
    return references


def _unique_imports(imports: Iterable[ImportReference]) -> list[ImportReference]:
    unique: dict[tuple[str, str, tuple[str, ...], int], ImportReference] = {}
    for reference in imports:
        key = (reference.path, reference.module, reference.names, reference.line)
        unique[key] = reference
    return list(unique.values())


def _unique_symbols(symbols: Iterable[Symbol]) -> list[Symbol]:
    """Deduplicate parser/fallback definitions while retaining best metadata."""

    unique: dict[tuple[str, str, str, int, int], Symbol] = {}
    for symbol in symbols:
        key = (symbol.path, symbol.name, symbol.kind, symbol.line_start, symbol.line_end)
        existing = unique.get(key)
        if existing is None or (not existing.signature and symbol.signature):
            unique[key] = symbol
    return list(unique.values())


def _build_module_map(files: Mapping[str, FileIndex]) -> dict[str, str]:
    result: dict[str, str] = {}
    for path, item in files.items():
        relative = Path(path)
        stem = relative.with_suffix("")
        if stem.name == "__init__":
            stem = stem.parent
        module = ".".join(stem.parts)
        result.setdefault(module, path)
        result.setdefault(relative.stem, path)
        if item.language in {"javascript", "typescript", "tsx"}:
            result.setdefault("./" + relative.with_suffix("").as_posix(), path)
    return result


def _resolve_import(
    root: Path,
    source_file: FileIndex,
    reference: ImportReference,
    files: Mapping[str, FileIndex],
    module_map: Mapping[str, str],
) -> str | None:
    module = reference.module.replace("\\", "/")
    source_path = Path(source_file.path)
    candidates: list[str] = []
    if source_file.language in {"javascript", "typescript", "tsx"} and module.startswith("."):
        base = (source_path.parent / module).as_posix()
        candidates.extend(_path_candidates(base, (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")))
        candidates.extend(f"{base}/{name}" for name in ("index.ts", "index.tsx", "index.js", "index.jsx"))
    elif source_file.language == "rust":
        if module.startswith("crate::"):
            module = module[7:]
        elif module.startswith("self::"):
            module = ".".join(Path(source_file.path).parent.parts) + "." + module[6:]
        elif module.startswith("super::"):
            module = ".".join(Path(source_file.path).parent.parent.parts) + "." + module[7:]
        module = module.replace("::", ".")
        parts = [part for part in module.split(".") if part]
        for end in range(len(parts), 0, -1):
            module_path = "/".join(parts[:end])
            for base in (module_path, f"src/{module_path}"):
                candidates.extend(_path_candidates(base, (".rs",)))
                candidates.append(f"{base}/mod.rs")
    elif source_file.language == "go":
        if module.startswith("."):
            candidates.extend(_path_candidates((source_path.parent / module).as_posix(), (".go",)))
        else:
            go_module = _go_module_name(root)
            if go_module and module.startswith(go_module + "/"):
                package = module[len(go_module) + 1 :].strip("/")
                candidates.extend(
                    path
                    for path, item in sorted(files.items())
                    if item.language == "go" and Path(path).parent.as_posix() in {package, package.rstrip("/")}
                )
    else:
        if reference.level:
            parent = source_path.parent
            for _ in range(max(0, reference.level - 1)):
                parent = parent.parent
            base_module = ".".join(parent.parts)
            module = f"{base_module}.{module}" if base_module and module else base_module or module
        candidates.extend(_path_candidates(module.replace(".", "/"), (".py", ".pyi", ".go", ".rs")))
        candidates.append(f"{module.replace('.', '/')}/__init__.py")
        if module in module_map:
            candidates.append(module_map[module])
        suffix = "." + module
        candidates.extend(path for key, path in module_map.items() if key.endswith(suffix))

    for candidate in candidates:
        try:
            normalized = _as_relative_path(candidate)
        except ValueError:
            continue
        if normalized in files:
            return normalized
    return None


def _go_module_name(root: Path) -> str:
    """Read only the local module declaration needed for Go import resolution."""

    try:
        for line in (root / "go.mod").read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(r"\s*module\s+(\S+)", line)
            if match:
                return match.group(1)
    except OSError:
        pass
    return ""


def _path_candidates(base: str, suffixes: Sequence[str]) -> list[str]:
    path = Path(base)
    candidates = [path.as_posix()]
    if path.suffix == "":
        candidates.extend(path.with_suffix(suffix).as_posix() for suffix in suffixes)
    return candidates


__all__ = [
    "FileIndex",
    "ImportReference",
    "IndexedFile",
    "LANGUAGE_BY_SUFFIX",
    "RepositoryIndex",
    "RepositoryIndexer",
    "Symbol",
    "SymbolDefinition",
    "build_index",
    "index_repository",
]

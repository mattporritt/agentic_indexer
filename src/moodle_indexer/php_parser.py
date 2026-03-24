"""PHP parsing and fallback extraction helpers.

Phase 1 uses ``phply`` for parser-based symbol extraction where practical and
adds light regex fallback logic for resilience when a file contains syntax the
library does not understand.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

LOGGER = logging.getLogger(__name__)

try:
    from phply import phplex, phpparse
except ModuleNotFoundError:  # pragma: no cover - exercised indirectly in tests
    phplex = None
    phpparse = None


NAMESPACE_RE = re.compile(r"namespace\s+([^;]+);")
DECLARATION_RE = re.compile(
    r"""
    (?P<kind>class|interface|trait)\s+
    (?P<name>[A-Za-z_][A-Za-z0-9_]*)              # symbol name
    (?:\s+extends\s+(?P<extends>[\\A-Za-z_][\\A-Za-z0-9_]*))?
    (?:\s+implements\s+(?P<implements>[\\A-Za-z0-9_,\s]+))?
    \s*\{
    """,
    re.VERBOSE,
)
FUNCTION_RE = re.compile(r"\bfunction\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
METHOD_RE = re.compile(
    r"""
    (?:
        public|protected|private|static|abstract|final|\s
    )*
    function\s+
    (?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(
    """,
    re.VERBOSE,
)


@dataclass(slots=True)
class ParsedMethod:
    """A method discovered in PHP code."""

    name: str
    line: int


@dataclass(slots=True)
class ParsedSymbol:
    """A class, interface, trait, or function discovered in PHP code."""

    symbol_type: str
    name: str
    fqname: str
    line: int
    namespace: str | None = None
    extends: str | None = None
    implements: list[str] = field(default_factory=list)
    methods: list[ParsedMethod] = field(default_factory=list)


def parse_php_symbols(source: str) -> list[ParsedSymbol]:
    """Parse PHP symbols with ``phply`` and fall back to regex if needed."""

    if phplex is not None and phpparse is not None:
        try:
            lexer = phplex.lexer.clone()
            parser = phpparse.make_parser()
            ast = parser.parse(source, lexer=lexer, debug=False)
            parsed = _extract_from_ast(ast or [], source)
            if parsed:
                return parsed
        except Exception:
            LOGGER.debug("Falling back to regex PHP extraction", exc_info=True)
    return _extract_with_regex_fallback(source)


def _extract_from_ast(ast_nodes: list[Any], source: str) -> list[ParsedSymbol]:
    """Extract symbol data from a ``phply`` AST."""

    namespace = _extract_namespace_from_source(source)
    results: list[ParsedSymbol] = []
    for node in ast_nodes:
        _walk_ast_node(node, namespace, results)
    return results


def _walk_ast_node(node: Any, namespace: str | None, results: list[ParsedSymbol]) -> None:
    """Recursively walk ``phply`` nodes and collect symbol definitions."""

    if node is None:
        return

    node_type = type(node).__name__
    if node_type == "Namespace":
        child_namespace = getattr(node, "name", None) or namespace
        for child in getattr(node, "nodes", []) or []:
            _walk_ast_node(child, child_namespace, results)
        return

    if node_type in {"Class", "Interface", "Trait"}:
        name = getattr(node, "name", None)
        line = _safe_line(node)
        fqname = _qualify_name(namespace, name)
        methods = [
            ParsedMethod(name=getattr(method, "name", "unknown"), line=_safe_line(method))
            for method in getattr(node, "nodes", []) or []
            if type(method).__name__ == "Method"
        ]
        extends = _node_name(getattr(node, "extends", None))
        implements = [_node_name(item) for item in getattr(node, "implements", []) or [] if _node_name(item)]
        results.append(
            ParsedSymbol(
                symbol_type=node_type.lower(),
                name=name,
                fqname=fqname,
                line=line,
                namespace=namespace,
                extends=extends,
                implements=implements,
                methods=methods,
            )
        )
        return

    if node_type == "Function":
        name = getattr(node, "name", None)
        line = _safe_line(node)
        results.append(
            ParsedSymbol(
                symbol_type="function",
                name=name,
                fqname=_qualify_name(namespace, name),
                line=line,
                namespace=namespace,
            )
        )
        return

    for child in _iter_child_nodes(node):
        _walk_ast_node(child, namespace, results)


def _iter_child_nodes(node: Any) -> list[Any]:
    """Yield possible child nodes from a generic AST node."""

    children: list[Any] = []
    for value in vars(node).values():
        if isinstance(value, list):
            children.extend(item for item in value if hasattr(item, "__dict__"))
        elif hasattr(value, "__dict__"):
            children.append(value)
    return children


def _extract_with_regex_fallback(source: str) -> list[ParsedSymbol]:
    """Fallback extraction for files the parser cannot handle."""

    namespace = _extract_namespace_from_source(source)
    results: list[ParsedSymbol] = []
    seen_names: set[tuple[str, str, str | None]] = set()
    class_ranges: list[tuple[int, int]] = []

    for match in DECLARATION_RE.finditer(source):
        symbol_type = match.group("kind").lower()
        name = match.group("name")
        line = source.count("\n", 0, match.start()) + 1
        key = (symbol_type, name, namespace)
        if key in seen_names:
            continue
        seen_names.add(key)
        class_end = _find_matching_brace(source, match.end() - 1)
        body = source[match.end():class_end] if class_end > match.end() else ""
        methods = [
            ParsedMethod(
                name=method_match.group("name"),
                line=source.count("\n", 0, match.end() + method_match.start()) + 1,
            )
            for method_match in METHOD_RE.finditer(body)
            if method_match.group("name") != "__construct" or "__construct" in body
        ]
        extends = match.group("extends")
        implements = [
            item.strip()
            for item in (match.group("implements") or "").split(",")
            if item.strip()
        ]
        results.append(
            ParsedSymbol(
                symbol_type=symbol_type,
                name=name,
                fqname=_qualify_name(namespace, name),
                line=line,
                namespace=namespace,
                extends=extends,
                implements=implements,
                methods=methods,
            )
        )
        class_ranges.append((match.start(), class_end))

    for match in FUNCTION_RE.finditer(source):
        name = match.group(1)
        if _offset_within_ranges(match.start(), class_ranges):
            continue
        line = source.count("\n", 0, match.start()) + 1
        key = ("function", name, namespace)
        if key in seen_names:
            continue
        seen_names.add(key)
        results.append(
            ParsedSymbol(
                symbol_type="function",
                name=name,
                fqname=_qualify_name(namespace, name),
                line=line,
                namespace=namespace,
            )
        )

    return results


def _find_matching_brace(source: str, opening_brace_index: int) -> int:
    """Return the index of the matching closing brace for a declaration."""

    depth = 0
    for index in range(opening_brace_index, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return len(source)


def _offset_within_ranges(offset: int, ranges: list[tuple[int, int]]) -> bool:
    """Return whether an offset lies within any class or trait body range."""

    return any(start <= offset <= end for start, end in ranges)


def _qualify_name(namespace: str | None, name: str | None) -> str:
    """Build a Moodle-friendly fully qualified name."""

    if not name:
        return ""
    if namespace:
        return f"{namespace}\\{name}"
    return name


def _extract_namespace_from_source(source: str) -> str | None:
    """Extract the declared namespace from source text."""

    match = NAMESPACE_RE.search(source)
    return match.group(1).strip() if match else None


def _safe_line(node: Any) -> int:
    """Return the best available line number for a ``phply`` node."""

    lineno = getattr(node, "lineno", None)
    return int(lineno or 1)


def _node_name(node: Any) -> str | None:
    """Extract a readable symbol name from a ``phply`` child node."""

    if node is None:
        return None
    if isinstance(node, str):
        return node
    if hasattr(node, "name"):
        return str(getattr(node, "name"))
    return str(node)

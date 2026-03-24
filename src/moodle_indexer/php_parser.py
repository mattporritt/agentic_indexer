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
CLASS_RE = re.compile(r"\b(class|interface|trait)\s+([A-Za-z_][A-Za-z0-9_]*)")
FUNCTION_RE = re.compile(r"\bfunction\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")


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
    seen_names: set[tuple[str, str]] = set()

    for match in CLASS_RE.finditer(source):
        symbol_type, name = match.groups()
        line = source.count("\n", 0, match.start()) + 1
        key = (symbol_type.lower(), name)
        if key in seen_names:
            continue
        seen_names.add(key)
        results.append(
            ParsedSymbol(
                symbol_type=symbol_type.lower(),
                name=name,
                fqname=_qualify_name(namespace, name),
                line=line,
                namespace=namespace,
            )
        )

    for match in FUNCTION_RE.finditer(source):
        name = match.group(1)
        line = source.count("\n", 0, match.start()) + 1
        key = ("function", name)
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

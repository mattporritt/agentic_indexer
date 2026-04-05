"""PHP parsing and fallback extraction helpers.

The parser uses ``phply`` where practical and adds light regex fallback logic
for resilience when a file contains syntax the library does not understand.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
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
    (?P<docblock>/\*\*.*?\*/\s*)?
    (?P<modifiers>(?:(?:abstract|final)\s+)*)?
    (?P<kind>class|interface|trait)\s+
    (?P<name>[A-Za-z_][A-Za-z0-9_]*)              # symbol name
    (?:\s+extends\s+(?P<extends>[\\A-Za-z_][\\A-Za-z0-9_]*))?
    (?:\s+implements\s+(?P<implements>[\\A-Za-z0-9_,\s]+))?
    \s*\{
    """,
    re.VERBOSE | re.DOTALL,
)
CALLABLE_START_RE = re.compile(
    r"""
    (?P<docblock>/\*\*.*?\*/\s*)?
    (?P<modifiers>(?:(?:public|protected|private|static|abstract|final)\s+)*)?
    function\s+
    (?P<reference>&\s*)?
    (?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*
    \(
    """,
    re.VERBOSE | re.DOTALL,
)


@dataclass(slots=True)
class ParsedMethod:
    """A method discovered in PHP code."""

    name: str
    line: int
    signature: str | None = None
    parameters: list[dict[str, str | None]] = field(default_factory=list)
    return_type: str | None = None
    docblock_summary: str | None = None
    docblock_tags: dict[str, list[str]] = field(default_factory=dict)
    visibility: str | None = None
    is_static: bool = False
    is_final: bool = False
    is_abstract: bool = False


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
    signature: str | None = None
    parameters: list[dict[str, str | None]] = field(default_factory=list)
    return_type: str | None = None
    docblock_summary: str | None = None
    docblock_tags: dict[str, list[str]] = field(default_factory=dict)
    visibility: str | None = None
    is_static: bool = False
    is_final: bool = False
    is_abstract: bool = False


def parse_php_symbols(source: str) -> list[ParsedSymbol]:
    """Parse PHP symbols with ``phply`` and fall back to regex if needed."""

    regex_symbols = _extract_with_regex_fallback(source)
    if phplex is not None and phpparse is not None:
        try:
            lexer = phplex.lexer.clone()
            parser = phpparse.make_parser()
            ast = parser.parse(source, lexer=lexer, debug=False)
            parsed = _extract_from_ast(ast or [], source)
            if parsed:
                return _merge_symbol_metadata(parsed, regex_symbols)
        except Exception:
            LOGGER.debug("Falling back to regex PHP extraction", exc_info=True)
    return regex_symbols


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
            _parsed_method_from_match(source, match.end(), method_match)
            for method_match in _iter_callable_declarations(body)
            if method_match["name"] != "__construct" or "__construct" in body
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
                signature=_build_type_signature(symbol_type, name, extends, implements, match.group("modifiers")),
                docblock_summary=_docblock_summary(match.group("docblock")),
                docblock_tags=_docblock_tags(match.group("docblock")),
                is_final="final" in _modifier_tokens(match.group("modifiers")),
                is_abstract=symbol_type == "interface" or "abstract" in _modifier_tokens(match.group("modifiers")),
            )
        )
        class_ranges.append((match.start(), class_end))

    for match in _iter_callable_declarations(source):
        name = str(match["name"])
        if _offset_within_ranges(int(match["start"]), class_ranges):
            continue
        line = source.count("\n", 0, int(match["start"])) + 1
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
                signature=_build_function_signature(name, _string_value(match["params"]), _string_value(match["return_type"]), _string_value(match["modifiers"])),
                parameters=_parse_parameters(_string_value(match["params"])),
                return_type=_clean_type(_string_value(match["return_type"])),
                docblock_summary=_docblock_summary(_string_value(match["docblock"])),
                docblock_tags=_docblock_tags(_string_value(match["docblock"])),
                visibility=_default_visibility(_string_value(match["modifiers"])),
                is_static="static" in _modifier_tokens(_string_value(match["modifiers"])),
                is_final="final" in _modifier_tokens(_string_value(match["modifiers"])),
                is_abstract="abstract" in _modifier_tokens(_string_value(match["modifiers"])),
            )
        )

    return results


def _find_matching_brace(source: str, opening_brace_index: int) -> int:
    """Return the index of the matching closing brace for a declaration."""

    return _find_matching_delimiter(source, opening_brace_index, "{", "}")


def _merge_symbol_metadata(parsed_symbols: list[ParsedSymbol], regex_symbols: list[ParsedSymbol]) -> list[ParsedSymbol]:
    """Merge parser-derived structure with regex-derived declaration metadata.

    ``phply`` can sometimes recover only part of a large legacy class body. When
    that happens we still want regex-discovered methods that are missing from the
    AST result to survive indexing, rather than disappearing from the merged
    symbol set.
    """

    regex_by_fqname = {item.fqname: item for item in regex_symbols if item.fqname}
    merged: list[ParsedSymbol] = []
    seen_fqnames: set[str] = set()

    for item in parsed_symbols:
        metadata = regex_by_fqname.get(item.fqname)
        if metadata is None:
            merged.append(item)
            seen_fqnames.add(item.fqname)
            continue

        method_metadata = {method.name: method for method in metadata.methods}
        merged_methods = [
            replace(
                method,
                signature=method_metadata.get(method.name).signature if method_metadata.get(method.name) else method.signature,
                parameters=method_metadata.get(method.name).parameters if method_metadata.get(method.name) else method.parameters,
                return_type=method_metadata.get(method.name).return_type if method_metadata.get(method.name) else method.return_type,
                docblock_summary=(
                    method_metadata.get(method.name).docblock_summary
                    if method_metadata.get(method.name)
                    else method.docblock_summary
                ),
                docblock_tags=method_metadata.get(method.name).docblock_tags if method_metadata.get(method.name) else method.docblock_tags,
                visibility=method_metadata.get(method.name).visibility if method_metadata.get(method.name) else method.visibility,
                is_static=(
                    method_metadata.get(method.name).is_static if method_metadata.get(method.name) else method.is_static
                ),
                is_final=method_metadata.get(method.name).is_final if method_metadata.get(method.name) else method.is_final,
                is_abstract=(
                    method_metadata.get(method.name).is_abstract if method_metadata.get(method.name) else method.is_abstract
                ),
            )
            for method in item.methods
        ]
        merged_method_names = {method.name for method in merged_methods}
        merged_methods.extend(
            method
            for method in metadata.methods
            if method.name not in merged_method_names
        )

        merged.append(
            replace(
                item,
                signature=metadata.signature or item.signature,
                parameters=metadata.parameters or item.parameters,
                return_type=metadata.return_type or item.return_type,
                docblock_summary=metadata.docblock_summary or item.docblock_summary,
                docblock_tags=metadata.docblock_tags or item.docblock_tags,
                visibility=metadata.visibility or item.visibility,
                is_static=metadata.is_static or item.is_static,
                is_final=metadata.is_final or item.is_final,
                is_abstract=metadata.is_abstract or item.is_abstract,
                methods=merged_methods or metadata.methods,
            )
        )
        seen_fqnames.add(item.fqname)

    for item in regex_symbols:
        if item.fqname not in seen_fqnames:
            merged.append(item)

    return merged


def _parsed_method_from_match(source: str, body_offset: int, method_match: dict[str, str | int | None]) -> ParsedMethod:
    """Build a detailed parsed-method record from a regex declaration match."""

    modifiers = _modifier_tokens(_string_value(method_match["modifiers"]))
    name = str(method_match["name"])
    return ParsedMethod(
        name=name,
        line=source.count("\n", 0, body_offset + int(method_match["start"])) + 1,
        signature=_build_function_signature(name, _string_value(method_match["params"]), _string_value(method_match["return_type"]), _string_value(method_match["modifiers"])),
        parameters=_parse_parameters(_string_value(method_match["params"])),
        return_type=_clean_type(_string_value(method_match["return_type"])),
        docblock_summary=_docblock_summary(_string_value(method_match["docblock"])),
        docblock_tags=_docblock_tags(_string_value(method_match["docblock"])),
        visibility=_default_visibility(_string_value(method_match["modifiers"])),
        is_static="static" in modifiers,
        is_final="final" in modifiers,
        is_abstract="abstract" in modifiers,
    )


def _iter_callable_declarations(source: str) -> list[dict[str, str | int | None]]:
    """Return callable declarations with balanced parameter extraction."""

    declarations: list[dict[str, str | int | None]] = []
    for match in CALLABLE_START_RE.finditer(source):
        params_start = match.end() - 1
        params_end = _find_matching_delimiter(source, params_start, "(", ")")
        if params_end <= params_start:
            continue
        cursor = params_end + 1
        while cursor < len(source) and source[cursor].isspace():
            cursor += 1
        return_type = None
        if cursor < len(source) and source[cursor] == ":":
            cursor += 1
            return_start = cursor
            while cursor < len(source) and source[cursor] not in "{;\n":
                cursor += 1
            return_type = source[return_start:cursor].strip() or None
        declarations.append(
            {
                "start": match.start(),
                "name": match.group("name"),
                "docblock": match.group("docblock"),
                "modifiers": match.group("modifiers"),
                "params": source[params_start + 1 : params_end],
                "return_type": return_type,
            }
        )
    return declarations


def _find_matching_delimiter(source: str, opening_index: int, opening: str, closing: str) -> int:
    """Return the matching closing delimiter index for a balanced pair."""

    depth = 0
    quote: str | None = None
    index = opening_index
    while index < len(source):
        char = source[index]
        previous = source[index - 1] if index > 0 else ""
        if quote:
            if char == quote and previous != "\\":
                quote = None
            index += 1
            continue
        if source.startswith("//", index) or char == "#":
            newline_index = source.find("\n", index)
            if newline_index == -1:
                return len(source)
            index = newline_index + 1
            continue
        if source.startswith("/*", index):
            comment_end = source.find("*/", index + 2)
            if comment_end == -1:
                return len(source)
            index = comment_end + 2
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return len(source)


def _string_value(value: str | int | None) -> str | None:
    """Return a string-like helper value or ``None``."""

    if value is None or isinstance(value, int):
        return None
    return value


def _offset_within_ranges(offset: int, ranges: list[tuple[int, int]]) -> bool:
    """Return whether an offset lies within any class or trait body range."""

    return any(start <= offset <= end for start, end in ranges)


def _modifier_tokens(raw_modifiers: str | None) -> set[str]:
    """Return normalized declaration modifiers from a regex capture."""

    if not raw_modifiers:
        return set()
    return {item.strip() for item in raw_modifiers.split() if item.strip()}


def _default_visibility(raw_modifiers: str | None) -> str | None:
    """Return the declared visibility, defaulting PHP methods to public."""

    modifiers = _modifier_tokens(raw_modifiers)
    for visibility in ("public", "protected", "private"):
        if visibility in modifiers:
            return visibility
    return "public" if raw_modifiers is not None else None


def _build_type_signature(
    symbol_type: str,
    name: str,
    extends: str | None,
    implements: list[str],
    raw_modifiers: str | None,
) -> str:
    """Return a compact declaration signature for a class/interface/trait."""

    prefixes = [item for item in (raw_modifiers or "").split() if item]
    signature = " ".join([*prefixes, symbol_type, name]).strip()
    if extends:
        signature = f"{signature} extends {extends}"
    if implements:
        signature = f"{signature} implements {', '.join(implements)}"
    return signature


def _build_function_signature(
    name: str,
    raw_params: str | None,
    raw_return_type: str | None,
    raw_modifiers: str | None,
) -> str:
    """Return a compact signature string for a function or method."""

    prefixes = [item for item in (raw_modifiers or "").split() if item]
    signature = " ".join([*prefixes, "function", f"{name}({_normalize_parameter_signature(raw_params)})"]).strip()
    return_type = _clean_type(raw_return_type)
    if return_type:
        signature = f"{signature}: {return_type}"
    return signature


def _normalize_parameter_signature(raw_params: str | None) -> str:
    """Normalize the parameter source into a compact signature fragment."""

    if not raw_params:
        return ""
    normalized = ", ".join(part.strip() for part in _split_parameters(raw_params))
    return normalized.rstrip(", ").strip()


def _parse_parameters(raw_params: str | None) -> list[dict[str, str | None]]:
    """Parse a PHP parameter list into structured parameter metadata."""

    if not raw_params:
        return []

    parameters: list[dict[str, str | None]] = []
    for raw_parameter in _split_parameters(raw_params):
        parameter = raw_parameter.strip()
        if not parameter:
            continue
        if "=" in parameter:
            declaration, default_value = parameter.split("=", 1)
            default = default_value.strip()
        else:
            declaration = parameter
            default = None
        declaration = declaration.strip()
        variable_match = re.search(r"(&?\.\.\.)?\$([A-Za-z_][A-Za-z0-9_]*)", declaration)
        if variable_match is None:
            continue
        prefix = variable_match.group(1) or ""
        name = variable_match.group(2)
        type_part = declaration[:variable_match.start()].strip()
        if prefix:
            type_part = f"{type_part} {prefix}".strip()
        parameters.append(
            {
                "name": name,
                "type": type_part or None,
                "default": default,
            }
        )
    return parameters


def _split_parameters(raw_params: str) -> list[str]:
    """Split a PHP parameter list while respecting nested delimiters and strings."""

    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None

    for char in raw_params:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            current.append(char)
            continue
        if char in {"(", "[", "{"}:
            depth += 1
        elif char in {")", "]", "}"} and depth > 0:
            depth -= 1
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current).strip())
    return parts


def _clean_type(raw_type: str | None) -> str | None:
    """Return a normalized type string."""

    if raw_type is None:
        return None
    cleaned = raw_type.strip()
    return cleaned or None


def _docblock_summary(raw_docblock: str | None) -> str | None:
    """Return the first descriptive line from a PHP docblock."""

    if not raw_docblock:
        return None
    for line in raw_docblock.splitlines():
        content = line.strip().lstrip("/*").strip()
        if not content or content.startswith("@"):
            continue
        return content
    return None


def _docblock_tags(raw_docblock: str | None) -> dict[str, list[str]]:
    """Return selected docblock tags from a PHP docblock."""

    tags: dict[str, list[str]] = {}
    if not raw_docblock:
        return tags
    for line in raw_docblock.splitlines():
        content = line.strip().lstrip("*").strip()
        if not content.startswith("@"):
            continue
        tag_name, _, remainder = content[1:].partition(" ")
        tags.setdefault(tag_name, []).append(remainder.strip())
    return tags


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

"""Moodle-specific extraction functions.

This module contains pragmatic Phase 1 extractors for PHP symbols, capability
definitions and checks, language strings and usages, and test artifacts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from moodle_indexer.file_roles import classify_file_role
from moodle_indexer.models import (
    CapabilityRecord,
    CapabilityUsageRecord,
    LanguageStringRecord,
    LanguageStringUsageRecord,
    RelationshipRecord,
    SymbolRecord,
    TestRecord,
)
from moodle_indexer.php_parser import ParsedSymbol, parse_php_symbols

CAPABILITY_CALL_RE = re.compile(r"""\b(has_capability|require_capability)\s*\(\s*['"]([^'"]+)['"]""")
STRING_DEF_RE = re.compile(r"\$string\['([^']+)'\]\s*=\s*'((?:\\'|[^'])*)';")
GET_STRING_RE = re.compile(r"\bget_string\s*\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)")
FEATURE_SCENARIO_RE = re.compile(r"^\s*(Feature|Scenario|Scenario Outline):\s*(.+)\s*$", re.MULTILINE)
CAPABILITIES_ASSIGNMENT_RE = re.compile(r"\$capabilities\s*=")


@dataclass(slots=True)
class PhpArrayEntry:
    """One key/value pair extracted from a PHP array literal."""

    key: str
    raw_value: str
    key_index: int
    value_start: int
    value_end: int


def extract_php_artifacts(
    source: str,
    relative_path: str,
    component_name: str,
) -> tuple[list[SymbolRecord], list[RelationshipRecord]]:
    """Extract PHP symbols and basic relationships from a PHP source file."""

    parsed_symbols = parse_php_symbols(source)
    symbols: list[SymbolRecord] = []
    relationships: list[RelationshipRecord] = []

    for parsed in parsed_symbols:
        symbols.append(_symbol_record_from_parsed(parsed, relative_path, component_name))
        if parsed.extends:
            relationships.append(
                RelationshipRecord(
                    source_fqname=parsed.fqname,
                    target_name=parsed.extends,
                    relationship_type="extends",
                    file_path=relative_path,
                    line=parsed.line,
                )
            )
        for implemented in parsed.implements:
            relationships.append(
                RelationshipRecord(
                    source_fqname=parsed.fqname,
                    target_name=implemented,
                    relationship_type="implements",
                    file_path=relative_path,
                    line=parsed.line,
                )
            )
        for method in parsed.methods:
            method_fqname = f"{parsed.fqname}::{method.name}" if parsed.fqname else method.name
            symbols.append(
                SymbolRecord(
                    name=method.name,
                    fqname=method_fqname,
                    symbol_type="method",
                    file_path=relative_path,
                    component_name=component_name,
                    line=method.line,
                    namespace=parsed.namespace,
                    container_name=parsed.fqname,
                )
            )
            relationships.append(
                RelationshipRecord(
                    source_fqname=parsed.fqname,
                    target_name=method.name,
                    relationship_type="defines_method",
                    file_path=relative_path,
                    line=method.line,
                )
            )
            relationships.append(
                RelationshipRecord(
                    source_fqname=method_fqname,
                    target_name=parsed.fqname,
                    relationship_type="method_of",
                    file_path=relative_path,
                    line=method.line,
                )
            )

    return symbols, relationships


def extract_capabilities(source: str, relative_path: str, component_name: str) -> list[CapabilityRecord]:
    """Extract capability definitions from ``db/access.php``."""

    if classify_file_role(relative_path) != "access_definition":
        return []

    capabilities: list[CapabilityRecord] = []
    for entry in _parse_capabilities_entries(source):
        metadata = _parse_capability_metadata(source, entry)
        capabilities.append(
            CapabilityRecord(
                name=entry.key,
                component_name=component_name,
                file_path=relative_path,
                line=source.count("\n", 0, entry.key_index) + 1,
                captype=metadata.get("captype"),
                contextlevel=metadata.get("contextlevel"),
                archetypes=metadata.get("archetypes", {}),
                riskbitmask=metadata.get("riskbitmask"),
                clonepermissionsfrom=metadata.get("clonepermissionsfrom"),
            )
        )
    return capabilities


def _parse_capabilities_entries(source: str) -> list[PhpArrayEntry]:
    """Return top-level capability entries from the ``$capabilities`` array."""

    match = CAPABILITIES_ASSIGNMENT_RE.search(source)
    if match is None:
        return []

    array_start = _locate_array_start(source, match.end())
    if array_start is None:
        return []
    array_end = _find_matching_delimiter(source, array_start)
    return _parse_php_array_entries(source, array_start, array_end)


def _parse_capability_metadata(source: str, entry: PhpArrayEntry) -> dict:
    """Return selected metadata fields from one top-level capability block."""

    value_start = _locate_array_start(source, entry.value_start, limit=entry.value_end)
    if value_start is None:
        return {}
    value_end = _find_matching_delimiter(source, value_start)
    child_entries = _parse_php_array_entries(source, value_start, value_end)
    metadata: dict[str, object] = {}

    for child in child_entries:
        normalized_key = child.key.lower()
        if normalized_key in {"captype", "contextlevel", "riskbitmask", "clonepermissionsfrom"}:
            metadata[normalized_key] = _normalize_php_scalar(child.raw_value)
        elif normalized_key == "archetypes":
            archetype_start = _locate_array_start(source, child.value_start, limit=child.value_end)
            if archetype_start is None:
                continue
            archetype_end = _find_matching_delimiter(source, archetype_start)
            archetype_entries = _parse_php_array_entries(source, archetype_start, archetype_end)
            metadata["archetypes"] = {
                item.key: _normalize_php_scalar(item.raw_value)
                for item in archetype_entries
            }

    return metadata


def _parse_php_array_entries(source: str, array_start: int, array_end: int) -> list[PhpArrayEntry]:
    """Return top-level key/value pairs from one PHP array literal."""

    entries: list[PhpArrayEntry] = []
    index = array_start + 1
    closing_delimiter = "]" if source[array_start] == "[" else ")"

    while index < array_end:
        index = _skip_php_noise(source, index, array_end)
        if index >= array_end or source[index] == closing_delimiter:
            break

        parsed_key = _parse_php_key(source, index, array_end)
        if parsed_key is None:
            break
        key, key_index, index = parsed_key
        index = _skip_php_noise(source, index, array_end)
        if source[index:index + 2] != "=>":
            break
        index += 2
        index = _skip_php_noise(source, index, array_end)
        value_start = index
        value_end = _consume_php_expression(source, index, array_end, closing_delimiter)
        entries.append(
            PhpArrayEntry(
                key=key,
                raw_value=source[value_start:value_end].strip(),
                key_index=key_index,
                value_start=value_start,
                value_end=value_end,
            )
        )
        index = value_end
        if index < array_end and source[index] == ",":
            index += 1

    return entries


def _locate_array_start(source: str, index: int, limit: int | None = None) -> int | None:
    """Return the opening delimiter for a PHP array value."""

    limit = len(source) if limit is None else limit
    index = _skip_php_noise(source, index, limit)
    if index >= limit:
        return None
    if source[index] == "[":
        return index
    if source.startswith("array", index):
        next_index = _skip_php_noise(source, index + len("array"), limit)
        if next_index < limit and source[next_index] == "(":
            return next_index
    return None


def _find_matching_delimiter(source: str, opening_index: int) -> int:
    """Return the matching closing delimiter for ``[``, ``(``, or ``{``."""

    opening_char = source[opening_index]
    closing_char = { "[": "]", "(": ")", "{": "}" }[opening_char]
    depth = 0
    index = opening_index

    while index < len(source):
        if source[index] in {"'", '"'}:
            index = _skip_php_string(source, index)
            continue
        if source.startswith("//", index) or source[index] == "#":
            index = _skip_php_line_comment(source, index)
            continue
        if source.startswith("/*", index):
            index = _skip_php_block_comment(source, index)
            continue

        char = source[index]
        if char == opening_char:
            depth += 1
        elif char == closing_char:
            depth -= 1
            if depth == 0:
                return index
        index += 1

    return len(source) - 1


def _parse_php_key(source: str, index: int, limit: int) -> tuple[str, int, int] | None:
    """Parse one PHP array key."""

    if source[index] in {"'", '"'}:
        value, next_index = _read_php_string(source, index)
        return value, index, next_index

    key_start = index
    while index < limit and not source.startswith("=>", index):
        if source[index] in ",)]":
            return None
        index += 1
    return source[key_start:index].strip(), key_start, index


def _consume_php_expression(source: str, index: int, limit: int, top_level_close: str) -> int:
    """Consume one PHP expression until the next top-level entry boundary."""

    stack: list[str] = []
    while index < limit:
        if source[index] in {"'", '"'}:
            index = _skip_php_string(source, index)
            continue
        if source.startswith("//", index) or source[index] == "#":
            index = _skip_php_line_comment(source, index)
            continue
        if source.startswith("/*", index):
            index = _skip_php_block_comment(source, index)
            continue

        char = source[index]
        if char in "[({":
            stack.append({ "[": "]", "(": ")", "{": "}" }[char])
            index += 1
            continue
        if stack and char == stack[-1]:
            stack.pop()
            index += 1
            continue
        if not stack and char in {",", top_level_close}:
            return index
        index += 1
    return limit


def _normalize_php_scalar(raw_value: str) -> str:
    """Normalize a lightweight PHP scalar or expression into a stable string."""

    value = raw_value.strip().rstrip(",")
    if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
        return _decode_php_string(value[1:-1], value[0])
    return value


def _skip_php_noise(source: str, index: int, limit: int) -> int:
    """Skip whitespace and comments within a bounded region."""

    while index < limit:
        if source[index].isspace():
            index += 1
            continue
        if source.startswith("//", index) or source[index] == "#":
            index = _skip_php_line_comment(source, index)
            continue
        if source.startswith("/*", index):
            index = _skip_php_block_comment(source, index)
            continue
        break
    return index


def _skip_php_string(source: str, index: int) -> int:
    """Return the first index after a quoted PHP string literal."""

    _, next_index = _read_php_string(source, index)
    return next_index


def _read_php_string(source: str, index: int) -> tuple[str, int]:
    """Read a quoted PHP string literal and return its decoded value."""

    quote = source[index]
    value_parts: list[str] = []
    index += 1
    while index < len(source):
        char = source[index]
        if char == "\\" and index + 1 < len(source):
            value_parts.append(source[index:index + 2])
            index += 2
            continue
        if char == quote:
            return _decode_php_string("".join(value_parts), quote), index + 1
        value_parts.append(char)
        index += 1
    return _decode_php_string("".join(value_parts), quote), index


def _decode_php_string(value: str, quote: str) -> str:
    """Decode a minimal subset of PHP string escapes used in fixtures/Moodle config."""

    value = value.replace(f"\\{quote}", quote)
    return value.replace("\\\\", "\\")


def _skip_php_line_comment(source: str, index: int) -> int:
    """Skip a ``//`` or ``#`` line comment."""

    while index < len(source) and source[index] != "\n":
        index += 1
    return index


def _skip_php_block_comment(source: str, index: int) -> int:
    """Skip a ``/* ... */`` block comment."""

    end_index = source.find("*/", index + 2)
    if end_index == -1:
        return len(source)
    return end_index + 2


def extract_capability_usages(source: str, relative_path: str, component_name: str) -> list[CapabilityUsageRecord]:
    """Extract obvious capability checks such as ``has_capability``."""

    usages: list[CapabilityUsageRecord] = []
    for match in CAPABILITY_CALL_RE.finditer(source):
        function_name, capability_name = match.groups()
        usages.append(
            CapabilityUsageRecord(
                capability_name=capability_name,
                function_name=function_name,
                file_path=relative_path,
                line=source.count("\n", 0, match.start()) + 1,
                component_name=component_name,
            )
        )
    return usages


def extract_language_strings(source: str, relative_path: str, component_name: str) -> list[LanguageStringRecord]:
    """Extract language string definitions from ``lang/en/*.php`` files."""

    if classify_file_role(relative_path) != "lang_file":
        return []

    strings: list[LanguageStringRecord] = []
    for match in STRING_DEF_RE.finditer(source):
        key, value = match.groups()
        strings.append(
            LanguageStringRecord(
                string_key=key,
                string_value=value.replace("\\'", "'"),
                component_name=component_name,
                file_path=relative_path,
                line=source.count("\n", 0, match.start()) + 1,
            )
        )
    return strings


def extract_language_string_usages(source: str, relative_path: str) -> list[LanguageStringUsageRecord]:
    """Extract obvious ``get_string`` calls."""

    usages: list[LanguageStringUsageRecord] = []
    for match in GET_STRING_RE.finditer(source):
        key, component = match.groups()
        usages.append(
            LanguageStringUsageRecord(
                string_key=key,
                component_name=component,
                file_path=relative_path,
                line=source.count("\n", 0, match.start()) + 1,
            )
        )
    return usages


def extract_tests(source: str, relative_path: str, component_name: str) -> list[TestRecord]:
    """Extract PHPUnit and Behat test artifacts from supported files."""

    role = classify_file_role(relative_path)
    tests: list[TestRecord] = []
    if role == "phpunit_test":
        symbols, _ = extract_php_artifacts(source, relative_path, component_name)
        for symbol in symbols:
            if symbol.symbol_type == "class":
                tests.append(
                    TestRecord(
                        name=symbol.fqname,
                        test_type="phpunit_class",
                        file_path=relative_path,
                        component_name=component_name,
                        line=symbol.line,
                    )
                )
        for line_number, line in enumerate(source.splitlines(), start=1):
            method_match = re.search(r"\bfunction\s+(test_[A-Za-z0-9_]+)\s*\(", line)
            if method_match:
                tests.append(
                    TestRecord(
                        name=method_match.group(1),
                        test_type="phpunit_method",
                        file_path=relative_path,
                        component_name=component_name,
                        line=line_number,
                    )
                )
    elif role == "behat_feature":
        for match in FEATURE_SCENARIO_RE.finditer(source):
            keyword, name = match.groups()
            tests.append(
                TestRecord(
                    name=name.strip(),
                    test_type=f"behat_{keyword.lower().replace(' ', '_')}",
                    file_path=relative_path,
                    component_name=component_name,
                    line=source.count("\n", 0, match.start()) + 1,
                )
            )
    elif role == "behat_context":
        symbols, _ = extract_php_artifacts(source, relative_path, component_name)
        for symbol in symbols:
            if symbol.symbol_type == "class":
                tests.append(
                    TestRecord(
                        name=symbol.fqname,
                        test_type="behat_context",
                        file_path=relative_path,
                        component_name=component_name,
                        line=symbol.line,
                    )
                )
    return tests


def _symbol_record_from_parsed(parsed: ParsedSymbol, relative_path: str, component_name: str) -> SymbolRecord:
    """Map a parsed PHP symbol into the shared storage model."""

    return SymbolRecord(
        name=parsed.name,
        fqname=parsed.fqname,
        symbol_type=parsed.symbol_type,
        file_path=relative_path,
        component_name=component_name,
        line=parsed.line,
        namespace=parsed.namespace,
    )


def is_php_file(path: Path) -> bool:
    """Return whether the given path is a PHP file."""

    return path.suffix.lower() == ".php"

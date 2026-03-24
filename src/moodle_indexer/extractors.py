"""Moodle-specific extraction functions.

This module contains pragmatic Phase 1 extractors for PHP symbols, capability
definitions and checks, language strings and usages, and test artifacts.
"""

from __future__ import annotations

import re
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


CAPABILITY_BLOCK_RE = re.compile(r"'([^']+/[^']+:[^']+)'\s*=>\s*\[(.*?)\n\s*\]", re.DOTALL)
CAPABILITY_FIELD_RE = re.compile(r"'(captype|contextlevel|riskbitmask)'\s*=>\s*([^,\n]+)")
ARCHETYPE_RE = re.compile(r"'archetypes'\s*=>\s*\[(.*?)\]", re.DOTALL)
ARCHETYPE_VALUE_RE = re.compile(r"'([^']+)'\s*=>\s*'([^']+)'")
CAPABILITY_CALL_RE = re.compile(r"\b(has_capability|required_capability)\s*\(\s*'([^']+)'")
STRING_DEF_RE = re.compile(r"\$string\['([^']+)'\]\s*=\s*'((?:\\'|[^'])*)';")
GET_STRING_RE = re.compile(r"\bget_string\s*\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)")
FEATURE_SCENARIO_RE = re.compile(r"^\s*(Feature|Scenario|Scenario Outline):\s*(.+)\s*$", re.MULTILINE)


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
            relationships.append(
                RelationshipRecord(
                    source_fqname=parsed.fqname,
                    target_name=method.name,
                    relationship_type="defines_method",
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
    for match in CAPABILITY_BLOCK_RE.finditer(source):
        name, block = match.groups()
        line = source.count("\n", 0, match.start()) + 1
        field_map = {field: value.strip().strip("',") for field, value in CAPABILITY_FIELD_RE.findall(block)}
        archetypes: dict[str, str] = {}
        archetypes_match = ARCHETYPE_RE.search(block)
        if archetypes_match:
            archetypes = {role: permission for role, permission in ARCHETYPE_VALUE_RE.findall(archetypes_match.group(1))}
        capabilities.append(
            CapabilityRecord(
                name=name,
                component_name=component_name,
                file_path=relative_path,
                line=line,
                captype=field_map.get("captype"),
                contextlevel=field_map.get("contextlevel"),
                archetypes=archetypes,
                riskbitmask=field_map.get("riskbitmask"),
            )
        )
    return capabilities


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

# Copyright (c) Moodle Pty Ltd. All rights reserved.
# Licensed under the Moodle Community License v1.3.
# See LICENSE.md in the repository root for full terms.
# Commercial use requires a separate written agreement with Moodle.

"""Moodle-specific extraction functions.

This module contains pragmatic Moodle-specific extractors for PHP symbols,
capability definitions and checks, language strings and usages, service
definitions, test artifacts, and AMD JavaScript module metadata.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from moodle_indexer.components import (
    infer_js_module_name,
    resolve_amd_build_path,
    resolve_classname_to_file_path,
)
from moodle_indexer.file_roles import classify_file_role
from moodle_indexer.js_modules import resolve_js_module_via_fallback
from moodle_indexer.models import (
    CapabilityRecord,
    CapabilityUsageRecord,
    JsImportRecord,
    JsModuleRecord,
    LanguageStringRecord,
    LanguageStringUsageRecord,
    RelationshipRecord,
    SymbolRecord,
    TestRecord,
    WebServiceRecord,
)
from moodle_indexer.php_parser import ParsedSymbol, parse_php_symbols

CAPABILITY_CALL_RE = re.compile(r"""\b(has_capability|require_capability)\s*\(\s*['"]([^'"]+)['"]""")
STRING_DEF_RE = re.compile(r"\$string\['([^']+)'\]\s*=\s*'((?:\\'|[^'])*)';")
GET_STRING_RE = re.compile(r"\bget_string\s*\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)")
FEATURE_SCENARIO_RE = re.compile(r"^\s*(Feature|Scenario|Scenario Outline):\s*(.+)\s*$", re.MULTILINE)
CAPABILITIES_ASSIGNMENT_RE = re.compile(r"\$capabilities\s*=")
FUNCTIONS_ASSIGNMENT_RE = re.compile(r"\$functions\s*=")
OUTPUT_CLASS_REFERENCE_RE = re.compile(r"(?<![A-Za-z0-9_])\\?([A-Za-z][A-Za-z0-9_]*_[A-Za-z0-9_]+\\output\\[A-Za-z0-9_\\]+)")
NEW_CLASS_REFERENCE_RE = re.compile(r"\bnew\s+([\\A-Za-z_][\\A-Za-z0-9_\\]*)")
NAMESPACE_RE = re.compile(r"namespace\s+([^;]+);")
IMPORT_FROM_RE = re.compile(
    r"""^\s*import\s+(?P<clause>.+?)\s+from\s+['"](?P<module>[^'"]+)['"]\s*;?\s*$""",
    re.MULTILINE,
)
IMPORT_SIDE_EFFECT_RE = re.compile(r"""^\s*import\s+['"](?P<module>[^'"]+)['"]\s*;?\s*$""", re.MULTILINE)
DEFINE_RE = re.compile(
    r"""define\s*\(\s*\[(?P<deps>.*?)\]\s*,\s*function\s*\((?P<params>[^)]*)\)""",
    re.DOTALL,
)
QUOTED_STRING_RE = re.compile(r"""['"]([^'"]+)['"]""")
EXPORT_DEFAULT_CLASS_RE = re.compile(
    r"""export\s+default\s+class(?:\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*))?(?:\s+extends\s+(?P<superclass>[A-Za-z_][A-Za-z0-9_]*))?""",
)
EXPORT_DEFAULT_ANONYMOUS_CLASS_RE = re.compile(
    r"""export\s+default\s+class\s+extends\s+(?P<superclass>[A-Za-z_][A-Za-z0-9_]*)"""
)
EXPORT_NAMED_CLASS_RE = re.compile(
    r"""export\s+class\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\s+extends\s+(?P<superclass>[A-Za-z_][A-Za-z0-9_]*))?"""
)
EXPORT_DEFAULT_RE = re.compile(r"""export\s+default\s+(?!class)(?P<name>[A-Za-z_][A-Za-z0-9_]*)""")


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
    namespace = _extract_namespace(source)
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
                    signature=method.signature,
                    parameters=method.parameters,
                    return_type=method.return_type,
                    docblock_summary=method.docblock_summary,
                    docblock_tags=method.docblock_tags,
                    visibility=method.visibility,
                    is_static=method.is_static,
                    is_final=method.is_final,
                    is_abstract=method.is_abstract,
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

    for match in OUTPUT_CLASS_REFERENCE_RE.finditer(source):
        class_name = match.group(1).lstrip("\\")
        relationships.append(
            RelationshipRecord(
                source_fqname=relative_path,
                target_name=class_name,
                relationship_type="references_class",
                file_path=relative_path,
                line=source.count("\n", 0, match.start()) + 1,
            )
        )
    for match in NEW_CLASS_REFERENCE_RE.finditer(source):
        normalized_reference = _normalize_class_reference(match.group(1), namespace)
        if normalized_reference is None or normalized_reference.endswith("\\output"):
            continue
        relationships.append(
            RelationshipRecord(
                source_fqname=relative_path,
                target_name=normalized_reference,
                relationship_type="references_class",
                file_path=relative_path,
                line=source.count("\n", 0, match.start()) + 1,
            )
        )

    return symbols, relationships


def extract_js_module_artifacts(
    source: str,
    relative_path: str,
    component_name: str,
) -> tuple[JsModuleRecord | None, list[JsImportRecord], list[RelationshipRecord]]:
    """Extract Moodle AMD source metadata from a JavaScript source file.

    The parser intentionally stays small and Moodle-specific:
    - modern ES module imports
    - legacy ``define([...], function(...))`` dependencies
    - default export / exported class detection
    - class inheritance linked back to imported module bindings where possible
    - deterministic ``amd/src`` to ``amd/build`` pairing
    """

    if classify_file_role(relative_path) != "amd_source":
        return None, [], []

    module_name = infer_js_module_name(relative_path, component_name)
    if module_name is None:
        return None, [], []

    imports, binding_modules = _extract_js_imports(source, relative_path, component_name)
    export_kind, export_name, superclass_name = _extract_js_export_metadata(source)
    superclass_module = binding_modules.get(superclass_name) if superclass_name else None
    resolved_superclass_file = (
        resolve_js_module_via_fallback(superclass_module, None).source_file if superclass_module else None
    )
    build_file = resolve_amd_build_path(relative_path)

    relationships: list[RelationshipRecord] = []
    for item in imports:
        relationships.append(
            RelationshipRecord(
                source_fqname=module_name,
                target_name=item.module_name,
                relationship_type="js_imports",
                file_path=relative_path,
                line=item.line,
            )
        )
    if superclass_name:
        relationships.append(
            RelationshipRecord(
                source_fqname=module_name,
                target_name=superclass_module or superclass_name,
                relationship_type="js_extends",
                file_path=relative_path,
                line=_line_number_for_match(source, EXPORT_DEFAULT_CLASS_RE.search(source) or EXPORT_NAMED_CLASS_RE.search(source)),
            )
        )
    if build_file:
        relationships.append(
            RelationshipRecord(
                source_fqname=module_name,
                target_name=build_file,
                relationship_type="builds_to",
                file_path=relative_path,
                line=1,
            )
        )

    js_module = JsModuleRecord(
        module_name=module_name,
        component_name=component_name,
        file_path=relative_path,
        export_kind=export_kind,
        export_name=export_name,
        superclass_name=superclass_name,
        superclass_module=superclass_module,
        resolved_superclass_file=resolved_superclass_file,
        build_file=build_file,
        build_status="resolved" if build_file else "unresolved",
    )
    return js_module, imports, relationships


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


def _extract_namespace(source: str) -> str | None:
    """Return the declared namespace for a PHP source file when present."""

    match = NAMESPACE_RE.search(source)
    return match.group(1).strip() if match else None


def _normalize_class_reference(reference: str, namespace: str | None) -> str | None:
    """Normalize a class reference into a resolvable Moodle class name."""

    if not reference:
        return None
    normalized = reference.lstrip("\\")
    if "\\" not in normalized:
        return normalized
    if reference.startswith("\\"):
        return normalized
    if namespace:
        return f"{namespace}\\{normalized}"
    return normalized


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


def extract_webservices(source: str, relative_path: str, component_name: str) -> list[WebServiceRecord]:
    """Extract service definitions from ``db/services.php``."""

    if classify_file_role(relative_path) != "services_definition":
        return []

    webservices: list[WebServiceRecord] = []
    for entry in _parse_functions_entries(source):
        metadata = _parse_service_metadata(source, entry)
        classpath = metadata.get("classpath")
        classname = metadata.get("classname")
        resolved_target_file, resolution_type, resolution_status = _resolve_service_target(
            component_name,
            classpath,
            classname,
        )
        webservices.append(
            WebServiceRecord(
                service_name=entry.key,
                component_name=component_name,
                file_path=relative_path,
                line=source.count("\n", 0, entry.key_index) + 1,
                classpath=classpath,
                classname=classname,
                methodname=metadata.get("methodname"),
                resolved_target_file=resolved_target_file,
                resolution_type=resolution_type,
                resolution_status=resolution_status,
            )
        )
    return webservices


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


def _extract_js_imports(
    source: str,
    relative_path: str,
    component_name: str,
) -> tuple[list[JsImportRecord], dict[str, str]]:
    """Return JS import/dependency records plus local-binding module lookup."""

    imports: list[JsImportRecord] = []
    binding_modules: dict[str, str] = {}

    for match in IMPORT_FROM_RE.finditer(source):
        module_name = match.group("module")
        line = source.count("\n", 0, match.start()) + 1
        resolution = resolve_js_module_via_fallback(module_name)
        resolved_target_file = resolution.source_file
        resolution_status = resolution.resolution_status
        clause = match.group("clause").strip()
        parsed_imports = _parse_import_clause(
            clause,
            module_name,
            line,
            relative_path,
            component_name,
            resolved_target_file,
            resolution_status,
        )
        imports.extend(parsed_imports)
        for item in parsed_imports:
            if item.local_name:
                binding_modules[item.local_name] = module_name

    for match in IMPORT_SIDE_EFFECT_RE.finditer(source):
        module_name = match.group("module")
        if any(item.module_name == module_name and item.line == source.count("\n", 0, match.start()) + 1 for item in imports):
            continue
        line = source.count("\n", 0, match.start()) + 1
        resolution = resolve_js_module_via_fallback(module_name)
        imports.append(
            JsImportRecord(
                module_name=module_name,
                file_path=relative_path,
                component_name=component_name,
                line=line,
                import_kind="side_effect",
                resolved_target_file=resolution.source_file,
                resolution_status=resolution.resolution_status,
            )
        )

    for match in DEFINE_RE.finditer(source):
        dependencies = [item.group(1) for item in QUOTED_STRING_RE.finditer(match.group("deps"))]
        factory_params = [item.strip() for item in match.group("params").split(",") if item.strip()]
        for index, module_name in enumerate(dependencies):
            line = source.count("\n", 0, match.start()) + 1
            resolution = resolve_js_module_via_fallback(module_name)
            local_name = factory_params[index] if index < len(factory_params) else None
            import_record = JsImportRecord(
                module_name=module_name,
                file_path=relative_path,
                component_name=component_name,
                line=line,
                import_kind="amd_dependency",
                local_name=local_name,
                resolved_target_file=resolution.source_file,
                resolution_status=resolution.resolution_status,
            )
            imports.append(import_record)
            if local_name:
                binding_modules[local_name] = module_name

    return imports, binding_modules


def _parse_import_clause(
    clause: str,
    module_name: str,
    line: int,
    relative_path: str,
    component_name: str,
    resolved_target_file: str | None,
    resolution_status: str,
) -> list[JsImportRecord]:
    """Parse one ES module import clause into concrete import records."""

    imports: list[JsImportRecord] = []

    def add(import_kind: str, imported_name: str | None, local_name: str | None) -> None:
        imports.append(
            JsImportRecord(
                module_name=module_name,
                file_path=relative_path,
                component_name=component_name,
                line=line,
                import_kind=import_kind,
                imported_name=imported_name,
                local_name=local_name,
                resolved_target_file=resolved_target_file,
                resolution_status=resolution_status,
            )
        )

    clause = clause.strip()
    if clause.startswith("{") and clause.endswith("}"):
        for item in _parse_named_imports(clause):
            add("named", item["imported_name"], item["local_name"])
        return imports

    if clause.startswith("* as "):
        add("namespace", None, clause.removeprefix("* as ").strip())
        return imports

    if "," in clause:
        default_part, rest = clause.split(",", 1)
        add("default", "default", default_part.strip())
        rest = rest.strip()
        if rest.startswith("{") and rest.endswith("}"):
            for item in _parse_named_imports(rest):
                add("named", item["imported_name"], item["local_name"])
        elif rest.startswith("* as "):
            add("namespace", None, rest.removeprefix("* as ").strip())
        return imports

    add("default", "default", clause)
    return imports


def _parse_named_imports(clause: str) -> list[dict[str, str]]:
    """Parse the names inside an ES module ``{ ... }`` import clause."""

    names = []
    for raw_item in clause.strip()[1:-1].split(","):
        item = raw_item.strip()
        if not item:
            continue
        if " as " in item:
            imported_name, local_name = [part.strip() for part in item.split(" as ", 1)]
        else:
            imported_name = item
            local_name = item
        names.append({"imported_name": imported_name, "local_name": local_name})
    return names


def _extract_js_export_metadata(source: str) -> tuple[str | None, str | None, str | None]:
    """Extract a compact export/superclass summary from one JS source file."""

    anonymous_default_class_match = EXPORT_DEFAULT_ANONYMOUS_CLASS_RE.search(source)
    if anonymous_default_class_match:
        return "default_class", None, anonymous_default_class_match.group("superclass")

    default_class_match = EXPORT_DEFAULT_CLASS_RE.search(source)
    if default_class_match:
        return "default_class", default_class_match.group("name"), default_class_match.group("superclass")

    named_class_match = EXPORT_NAMED_CLASS_RE.search(source)
    if named_class_match:
        return "named_class", named_class_match.group("name"), named_class_match.group("superclass")

    default_match = EXPORT_DEFAULT_RE.search(source)
    if default_match:
        return "default_reference", default_match.group("name"), None

    define_match = DEFINE_RE.search(source)
    if define_match:
        if re.search(r"\breturn\s+\{", source):
            return "amd_return_object", None, None
        if re.search(r"\breturn\s+function\b", source):
            return "amd_return_function", None, None
        return "amd_define", None, None

    return None, None, None


def _line_number_for_match(source: str, match: re.Match[str] | None) -> int:
    """Return a 1-based line number for a regex match, defaulting to line 1."""

    if match is None:
        return 1
    return source.count("\n", 0, match.start()) + 1


def _parse_functions_entries(source: str) -> list[PhpArrayEntry]:
    """Return top-level service entries from the ``$functions`` array."""

    match = FUNCTIONS_ASSIGNMENT_RE.search(source)
    if match is None:
        return []

    array_start = _locate_array_start(source, match.end())
    if array_start is None:
        return []
    array_end = _find_matching_delimiter(source, array_start)
    return _parse_php_array_entries(source, array_start, array_end)


def _parse_service_metadata(source: str, entry: PhpArrayEntry) -> dict[str, str]:
    """Return selected metadata fields from one service definition."""

    value_start = _locate_array_start(source, entry.value_start, limit=entry.value_end)
    if value_start is None:
        return {}
    value_end = _find_matching_delimiter(source, value_start)
    child_entries = _parse_php_array_entries(source, value_start, value_end)
    metadata: dict[str, str] = {}
    for child in child_entries:
        normalized_key = child.key.lower()
        if normalized_key in {"classpath", "classname", "methodname"}:
            metadata[normalized_key] = _normalize_php_scalar(child.raw_value)
    return metadata


def _resolve_service_target(
    component_name: str,
    classpath: str | None,
    classname: str | None,
) -> tuple[str | None, str, str]:
    """Resolve a service definition to its implementation file when practical."""

    if classpath:
        return Path(classpath).as_posix().lstrip("/"), "classpath", "resolved"
    if classname:
        resolved = resolve_classname_to_file_path(classname)
        if resolved is not None:
            return resolved, "classname", "resolved"
    return None, "unresolved", "unresolved"


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
        signature=parsed.signature,
        parameters=parsed.parameters,
        return_type=parsed.return_type,
        docblock_summary=parsed.docblock_summary,
        docblock_tags=parsed.docblock_tags,
        visibility=parsed.visibility,
        is_static=parsed.is_static,
        is_final=parsed.is_final,
        is_abstract=parsed.is_abstract,
    )


def is_php_file(path: Path) -> bool:
    """Return whether the given path is a PHP file."""

    return path.suffix.lower() == ".php"

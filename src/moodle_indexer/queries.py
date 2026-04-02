"""Query services over the SQLite index.

These functions implement the fixed Phase 1 CLI commands and keep SQL details
out of the command-line interface layer.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from moodle_indexer.components import resolve_classname_to_file_path, resolve_framework_class_to_file_path
from moodle_indexer.errors import ValidationError
from moodle_indexer.js_modules import JsModuleResolution, resolve_js_module
from moodle_indexer.paths import normalize_relative_lookup_path
from moodle_indexer.suggestions import suggest_related_files


@dataclass(slots=True)
class DefinitionCandidate:
    """A matched definition plus the context used to resolve it."""

    row: sqlite3.Row
    matched_via: str = "direct_definition"
    requested_container: str | None = None


def find_symbol(connection: sqlite3.Connection, symbol_name: str) -> dict:
    """Return symbol definitions and basic relationships for a name or fqname."""

    rows = connection.execute(
        """
        SELECT
            s.name,
            s.fqname,
            s.symbol_type,
            s.namespace,
            s.container_name,
            s.line,
            f.repository_relative_path,
            f.moodle_path,
            f.file_role,
            c.name AS component_name
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        JOIN components c ON c.id = s.component_id
        WHERE s.name = ? OR s.fqname = ?
        ORDER BY s.fqname, f.moodle_path, s.line
        """,
        (symbol_name, symbol_name),
    ).fetchall()

    matches = []
    for row in rows:
        outgoing = connection.execute(
            """
            SELECT relationship_type, target_name, line
            FROM relationships
            WHERE source_fqname = ?
            ORDER BY relationship_type, target_name, line
            """,
            (row["fqname"],),
        ).fetchall()
        incoming = connection.execute(
            """
            SELECT source_fqname, relationship_type, line
            FROM relationships
            WHERE target_name = ?
            ORDER BY relationship_type, source_fqname, line
            """,
            (row["fqname"],),
        ).fetchall()
        matches.append(
            {
                "name": row["name"],
                "fqname": row["fqname"],
                "symbol_type": row["symbol_type"],
                "namespace": row["namespace"],
                "container_name": row["container_name"],
                "component": row["component_name"],
                "file": row["moodle_path"],
                "repository_relative_path": row["repository_relative_path"],
                "file_role": row["file_role"],
                "line": row["line"],
                "relationships": [
                    {
                        "type": item["relationship_type"],
                        "target": item["target_name"],
                        "line": item["line"],
                    }
                    for item in outgoing
                ],
                "referenced_by": [
                    {
                        "source": item["source_fqname"],
                        "type": item["relationship_type"],
                        "line": item["line"],
                    }
                    for item in incoming
                ],
            }
        )
    return {"query": symbol_name, "matches": matches}


def find_definition(
    connection: sqlite3.Connection,
    symbol_query: str,
    symbol_type: str = "any",
    limit: int = 10,
    include_usages: bool = True,
) -> dict:
    """Return IDE-style definition records for functions, methods, and classes."""

    if "::" in symbol_query:
        matches = _find_method_definitions(connection, symbol_query, symbol_type, limit)
    else:
        matches = _find_named_definitions(connection, symbol_query, symbol_type, limit)

    results = []
    for candidate in matches[:limit]:
        payload = _serialize_definition_match(connection, candidate)
        if include_usages:
            payload["usage_examples"] = _find_usage_examples(connection, candidate.row, limit=min(5, limit))
            payload["usage_summary"] = _summarize_usage_examples(payload["usage_examples"])
        else:
            payload["usage_examples"] = []
            payload["usage_summary"] = {}
        results.append(payload)

    return {
        "query": symbol_query,
        "total_matches": len(matches),
        "matches": results,
    }


def file_context(connection: sqlite3.Connection, file_path: str) -> dict:
    """Return indexed metadata for one repository file.

    Query-time path resolution uses the repository metadata stored in SQLite so
    the CLI does not depend on a fresh ``--moodle-path`` argument after the
    index has been built.
    """

    repository = _get_indexed_repository_metadata(connection)
    row = _resolve_file_row(connection, repository, file_path)

    file_id = row["id"]
    moodle_path = row["moodle_path"]

    symbols = connection.execute(
        """
        SELECT name, fqname, symbol_type, namespace, line
        FROM symbols
        WHERE symbols.file_id = ?
        ORDER BY line, fqname
        """,
        (file_id,),
    ).fetchall()
    capabilities = connection.execute(
        """
        SELECT name, line, captype, contextlevel, archetypes_json, riskbitmask, clonepermissionsfrom
        FROM capabilities
        WHERE capabilities.file_id = ?
        ORDER BY name
        """,
        (file_id,),
    ).fetchall()
    strings = connection.execute(
        """
        SELECT string_key, string_value, line
        FROM language_strings
        WHERE language_strings.file_id = ?
        ORDER BY string_key
        """,
        (file_id,),
    ).fetchall()
    tests = connection.execute(
        """
        SELECT name, test_type, line
        FROM tests
        WHERE tests.file_id = ?
        ORDER BY test_type, name
        """,
        (file_id,),
    ).fetchall()
    capability_checks = connection.execute(
        """
        SELECT capability_name, function_name, line
        FROM capability_usages
        WHERE capability_usages.file_id = ?
        ORDER BY line, capability_name
        """,
        (file_id,),
    ).fetchall()
    string_usages = connection.execute(
        """
        SELECT string_key, component_name, line
        FROM language_string_usages
        WHERE language_string_usages.file_id = ?
        ORDER BY line, string_key
        """,
        (file_id,),
    ).fetchall()
    relationships = connection.execute(
        """
        SELECT source_fqname, target_name, relationship_type, line
        FROM relationships
        WHERE relationships.file_id = ?
        ORDER BY line, relationship_type, source_fqname, target_name
        """,
        (file_id,),
    ).fetchall()
    webservices = connection.execute(
        """
        SELECT
            service_name,
            line,
            classpath,
            classname,
            methodname,
            resolved_target_file,
            resolution_type,
            resolution_status
        FROM webservices
        WHERE webservices.file_id = ?
        ORDER BY service_name, line
        """,
        (file_id,),
    ).fetchall()
    js_module = connection.execute(
        """
        SELECT
            id,
            module_name,
            export_kind,
            export_name,
            superclass_name,
            superclass_module,
            resolved_superclass_file,
            build_file,
            build_status
        FROM js_modules
        WHERE file_id = ?
        """,
        (file_id,),
    ).fetchone()
    js_imports = []
    if js_module is not None:
        raw_js_imports = connection.execute(
            """
            SELECT
                module_name,
                line,
                import_kind,
                imported_name,
                local_name,
                resolved_target_file,
                resolution_status
            FROM js_imports
            WHERE js_module_id = ?
            ORDER BY line, module_name, local_name
            """,
            (js_module["id"],),
        ).fetchall()
        js_imports = [_serialize_js_import(connection, item) for item in raw_js_imports]
    linked_tests = _linked_service_tests(connection, webservices)
    class_references = _linked_class_artifacts(connection, relationships)
    rendering_references = [item for item in class_references if item["artifact_kind"] == "output_class"]

    related_suggestions = [
        {"path": item.path, "reason": item.reason}
        for item in suggest_related_files(moodle_path)
    ]
    related_suggestions.extend(_service_related_suggestions(webservices))
    related_suggestions.extend(_service_test_suggestions(connection, webservices))
    related_suggestions.extend(_class_related_suggestions(class_references))
    related_suggestions.extend(_js_related_suggestions(connection, js_module, js_imports))
    related_suggestions = _deduplicate_suggestions(related_suggestions)

    return {
        "file": moodle_path,
        "repository_relative_path": row["repository_relative_path"],
        "moodle_path": moodle_path,
        "path_scope": row["path_scope"],
        "absolute_path": str((Path(repository["repository_root"]) / row["repository_relative_path"]).resolve()),
        "component": row["component_name"],
        "file_role": row["file_role"],
        "extension": row["extension"],
        "symbols": [dict(item) for item in symbols],
        "capabilities": [
            {
                "name": item["name"],
                "line": item["line"],
                "captype": item["captype"],
                "contextlevel": item["contextlevel"],
                "archetypes": json.loads(item["archetypes_json"]),
                "riskbitmask": item["riskbitmask"],
                "clonepermissionsfrom": item["clonepermissionsfrom"],
            }
            for item in capabilities
        ],
        "language_strings": [dict(item) for item in strings],
        "capability_checks": [dict(item) for item in capability_checks],
        "string_usages": [dict(item) for item in string_usages],
        "tests": [
            {
                "name": item["name"],
                "test_type": item["test_type"],
                "line": item["line"],
                "file": moodle_path,
                "reason": None,
            }
            for item in tests
        ]
        + linked_tests,
        "webservices": [dict(item) for item in webservices],
        "js_module": _serialize_js_module(js_module, connection),
        "js_imports": js_imports,
        "class_references": class_references,
        "rendering_references": rendering_references,
        "relationships": [dict(item) for item in relationships],
        "related_suggestions": related_suggestions,
        "repository_root": repository["repository_root"],
        "application_root": repository["application_root"],
    }


def component_summary(connection: sqlite3.Connection, component_name: str) -> dict:
    """Return a compact summary of one Moodle component."""

    component = connection.execute(
        """
        SELECT id, name, component_type, root_path
        FROM components
        WHERE name = ?
        """,
        (component_name,),
    ).fetchone()
    if component is None:
        raise ValidationError(f"Component not found in index: {component_name}")

    files = connection.execute(
        """
        SELECT repository_relative_path, moodle_path, file_role
        FROM files
        WHERE component_id = ?
        ORDER BY moodle_path, repository_relative_path
        """,
        (component["id"],),
    ).fetchall()
    capabilities = connection.execute(
        """
        SELECT name, line, file_id, clonepermissionsfrom
        FROM capabilities
        WHERE component_id = ?
        ORDER BY name
        """,
        (component["id"],),
    ).fetchall()
    strings = connection.execute(
        """
        SELECT string_key, line
        FROM language_strings
        WHERE component_id = ?
        ORDER BY string_key
        """,
        (component["id"],),
    ).fetchall()
    tests = connection.execute(
        """
        SELECT name, test_type, line
        FROM tests
        WHERE component_id = ?
        ORDER BY test_type, name
        """,
        (component["id"],),
    ).fetchall()
    webservices = connection.execute(
        """
        SELECT
            service_name,
            line,
            classpath,
            classname,
            methodname,
            resolved_target_file,
            resolution_type,
            resolution_status
        FROM webservices
        WHERE component_id = ?
        ORDER BY service_name, line
        """,
        (component["id"],),
    ).fetchall()
    capability_checks = connection.execute(
        "SELECT capability_name, function_name, line FROM capability_usages WHERE component_id = ? ORDER BY capability_name, line",
        (component["id"],),
    ).fetchall()
    string_usages = connection.execute(
        "SELECT string_key, component_name, line FROM language_string_usages JOIN files ON files.id = language_string_usages.file_id WHERE files.component_id = ? ORDER BY string_key, line",
        (component["id"],),
    ).fetchall()
    symbols = connection.execute(
        """
        SELECT name, fqname, symbol_type, container_name, line
        FROM symbols
        WHERE component_id = ?
        ORDER BY symbol_type, fqname, line
        LIMIT 20
        """,
        (component["id"],),
    ).fetchall()
    relationship_count = connection.execute(
        "SELECT COUNT(*) AS count FROM relationships JOIN files ON files.id = relationships.file_id WHERE files.component_id = ?",
        (component["id"],),
    ).fetchone()["count"]

    role_counts: dict[str, int] = {}
    for file_row in files:
        role_counts[file_row["file_role"]] = role_counts.get(file_row["file_role"], 0) + 1

    return {
        "component": component["name"],
        "component_type": component["component_type"],
        "root_path": component["root_path"],
        "stats": {
            "file_count": len(files),
            "capability_count": len(capabilities),
            "language_string_count": len(strings),
            "test_count": len(tests),
            "webservice_count": len(webservices),
            "capability_check_count": len(capability_checks),
            "string_usage_count": len(string_usages),
            "relationship_count": relationship_count,
            "symbol_count": connection.execute(
                "SELECT COUNT(*) AS count FROM symbols WHERE component_id = ?",
                (component["id"],),
            ).fetchone()["count"],
        },
        "key_file_roles": dict(sorted(role_counts.items())),
        "files": [dict(item) for item in files],
        "capabilities": [
            {
                "name": item["name"],
                "line": item["line"],
                "clonepermissionsfrom": item["clonepermissionsfrom"],
            }
            for item in capabilities
        ],
        "language_strings": [{"string_key": item["string_key"], "line": item["line"]} for item in strings],
        "webservices": [dict(item) for item in webservices],
        "tests": [dict(item) for item in tests],
        "sample_symbols": [dict(item) for item in symbols],
    }


def suggest_related(connection: sqlite3.Connection, file_path: str) -> dict:
    """Return related-file suggestions for a repository file path."""

    repository = _get_indexed_repository_metadata(connection)
    row = _resolve_file_row(connection, repository, file_path)
    moodle_path = row["moodle_path"]

    suggestions = []
    for suggestion in suggest_related_files(moodle_path):
        exists = connection.execute(
            "SELECT 1 FROM files WHERE moodle_path = ? OR repository_relative_path = ?",
            (suggestion.path, suggestion.path),
        ).fetchone()
        suggestions.append(
            {
                "path": suggestion.path,
                "reason": suggestion.reason,
                "indexed": bool(exists),
            }
        )
    webservices = connection.execute(
        """
        SELECT service_name, classname, classpath, resolved_target_file, resolution_type
        FROM webservices
        WHERE file_id = ?
        ORDER BY service_name
        """,
        (row["id"],),
    ).fetchall()
    suggestions.extend(_indexed_service_suggestions(connection, webservices))
    suggestions.extend(_indexed_service_test_suggestions(connection, webservices))
    rendering_relationships = connection.execute(
        """
        SELECT target_name, relationship_type, line
        FROM relationships
        WHERE file_id = ? AND relationship_type IN ('references_class', 'extends')
        ORDER BY line, target_name
        """,
        (row["id"],),
    ).fetchall()
    suggestions.extend(_indexed_class_suggestions(connection, rendering_relationships))
    js_module = connection.execute(
        """
        SELECT
            id,
            module_name,
            export_kind,
            export_name,
            superclass_name,
            superclass_module,
            resolved_superclass_file,
            build_file,
            build_status
        FROM js_modules
        WHERE file_id = ?
        """,
        (row["id"],),
    ).fetchone()
    js_imports = []
    if js_module is not None:
        raw_js_imports = connection.execute(
            """
            SELECT module_name, line, import_kind, imported_name, local_name, resolved_target_file, resolution_status
            FROM js_imports
            WHERE js_module_id = ?
            ORDER BY line, module_name, local_name
            """,
            (js_module["id"],),
        ).fetchall()
        js_imports = [_serialize_js_import(connection, item) for item in raw_js_imports]
    suggestions.extend(_indexed_js_suggestions(connection, js_module, js_imports))
    suggestions = _deduplicate_indexed_suggestions(suggestions)
    return {
        "file": moodle_path,
        "repository_relative_path": row["repository_relative_path"],
        "moodle_path": moodle_path,
        "suggestions": suggestions,
    }


def _find_named_definitions(
    connection: sqlite3.Connection,
    symbol_query: str,
    symbol_type: str,
    limit: int,
) -> list[DefinitionCandidate]:
    """Return exact-name or fqname definition matches for non-method queries."""

    type_clause = ""
    parameters: list[object] = [symbol_query, symbol_query]
    if symbol_type != "any":
        type_clause = "AND s.symbol_type = ?"
        parameters.append(symbol_type)
    parameters.extend([symbol_query, symbol_query, symbol_query, limit])
    rows = connection.execute(
        f"""
        SELECT
            s.*,
            f.repository_relative_path,
            f.moodle_path,
            f.absolute_path,
            c.name AS component_name
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        JOIN components c ON c.id = s.component_id
        WHERE (s.name = ? OR s.fqname = ?)
          {type_clause}
        ORDER BY
            CASE
                WHEN s.fqname = ? THEN 0
                WHEN s.name = ? THEN 1
                WHEN s.fqname LIKE ? || '\\\\%' ESCAPE '\\' THEN 2
                ELSE 3
            END,
            s.symbol_type,
            s.fqname,
            s.line
        LIMIT ?
        """,
        tuple(parameters),
    ).fetchall()
    return [DefinitionCandidate(row=item) for item in rows]


def _find_method_definitions(
    connection: sqlite3.Connection,
    symbol_query: str,
    symbol_type: str,
    limit: int,
) -> list[DefinitionCandidate]:
    """Return ranked method matches for ``Class::method``-style queries."""

    class_part, method_name = symbol_query.split("::", 1)
    normalized_query = _normalize_php_symbol_name(symbol_query)
    normalized_class = _normalize_php_symbol_name(class_part)
    normalized_method = _normalize_php_symbol_name(method_name)
    rows = connection.execute(
        """
        SELECT
            s.*,
            f.repository_relative_path,
            f.moodle_path,
            f.absolute_path,
            c.name AS component_name
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        JOIN components c ON c.id = s.component_id
        WHERE s.symbol_type = 'method'
          AND s.name = ?
        ORDER BY s.fqname, s.line
        """,
        (method_name,),
    ).fetchall()

    ranked: list[tuple[tuple[int, str, int], sqlite3.Row]] = []
    for row in rows:
        if symbol_type not in {"any", "method"}:
            continue
        container_name = row["container_name"] or ""
        normalized_container = _normalize_php_symbol_name(container_name)
        container_short = normalized_container.split("\\")[-1] if normalized_container else None
        normalized_fqname = _normalize_php_symbol_name(row["fqname"])
        if normalized_fqname == normalized_query:
            rank = 0
        elif normalized_container == normalized_class:
            rank = 1
        elif normalized_container.endswith(f"\\{normalized_class}"):
            rank = 2
        elif container_short == normalized_class:
            rank = 3
        else:
            continue
        ranked.append(((rank, normalized_fqname, row["line"]), row))

    ranked.sort(key=lambda item: item[0])
    direct_matches = [DefinitionCandidate(row=row) for _, row in ranked[:limit]]
    if direct_matches:
        return direct_matches

    class_symbol = _find_class_symbol(connection, class_part)
    if class_symbol is None:
        return []

    inherited_method = _find_inherited_method_definition(connection, class_symbol, normalized_method)
    if inherited_method is None:
        return []
    return [
        DefinitionCandidate(
            row=inherited_method,
            matched_via="inherited_definition",
            requested_container=class_symbol["fqname"],
        )
    ]


def _normalize_php_symbol_name(name: str | None) -> str:
    """Return a normalized PHP symbol name for legacy and namespaced matching."""

    if not name:
        return ""
    normalized = str(name).strip()
    normalized = re.sub(r"\\{2,}", r"\\", normalized)
    return normalized.lstrip("\\")


def _serialize_definition_match(connection: sqlite3.Connection, candidate: DefinitionCandidate) -> dict:
    """Return one IDE-style definition payload."""

    row = candidate.row
    inheritance = (
        _method_inheritance_context(connection, candidate)
        if row["symbol_type"] == "method"
        else {}
    )
    return {
        "symbol_type": row["symbol_type"],
        "name": row["name"],
        "fqname": row["fqname"],
        "component": row["component_name"],
        "file": row["moodle_path"],
        "repository_relative_path": row["repository_relative_path"],
        "line": row["line"],
        "namespace": row["namespace"],
        "class_name": row["container_name"],
        "signature": row["signature"],
        "parameters": json.loads(row["parameters_json"]),
        "return_type": row["return_type"],
        "docblock_summary": row["docblock_summary"],
        "docblock_tags": json.loads(row["docblock_tags_json"]),
        "visibility": row["visibility"],
        "is_static": bool(row["is_static"]),
        "is_final": bool(row["is_final"]),
        "is_abstract": bool(row["is_abstract"]),
        "matched_via": candidate.matched_via,
        "requested_class_name": candidate.requested_container,
        "inheritance_role": inheritance.get("inheritance_role"),
        "overrides": inheritance.get("overrides"),
        "implements_method": inheritance.get("implements_method"),
        "parent_class": inheritance.get("parent_class"),
        "interface_names": inheritance.get("interface_names", []),
        "parent_definition": inheritance.get("parent_definition"),
        "overrides_definition": inheritance.get("overrides_definition"),
        "implements_definitions": inheritance.get("implements_definitions", []),
        "child_overrides": inheritance.get("child_overrides", []),
    }


def _method_inheritance_context(connection: sqlite3.Connection, candidate: DefinitionCandidate) -> dict:
    """Return practical Phase 2 inheritance context for a method definition."""

    row = candidate.row
    container_name = row["container_name"]
    if not container_name:
        return {
            "inheritance_role": "unknown",
            "overrides": None,
            "implements_method": [],
            "parent_class": None,
            "interface_names": [],
            "parent_definition": None,
            "overrides_definition": None,
            "implements_definitions": [],
            "child_overrides": [],
        }

    relationships = connection.execute(
        """
        SELECT relationship_type, target_name
        FROM relationships
        WHERE source_fqname = ?
          AND relationship_type IN ('extends', 'implements')
        ORDER BY relationship_type, target_name
        """,
        (container_name,),
    ).fetchall()

    parent_class = next(
        (
            _normalize_php_symbol_name(item["target_name"])
            for item in relationships
            if item["relationship_type"] == "extends"
        ),
        None,
    )
    interface_names = [
        _normalize_php_symbol_name(item["target_name"])
        for item in relationships
        if item["relationship_type"] == "implements"
    ]

    parent_definition = None
    overrides = None
    overrides_definition = None
    if parent_class:
        parent_method = _find_method_in_container(connection, parent_class, row["name"])
        if parent_method is not None:
            overrides = parent_method["fqname"]
            overrides_definition = _serialize_related_definition(parent_method)
            parent_definition = overrides_definition

    implemented_methods: list[str] = []
    implements_definitions: list[dict[str, object]] = []
    for interface_name in interface_names:
        interface_method = _find_method_in_container(connection, interface_name, row["name"])
        if interface_method is not None:
            implemented_methods.append(interface_method["fqname"])
            implements_definitions.append(_serialize_related_definition(interface_method))

    child_overrides = _find_child_override_definitions(connection, row, limit=5)

    if candidate.matched_via == "inherited_definition":
        inheritance_role = "inherited_not_overridden"
        parent_definition = _serialize_related_definition(row)
    elif overrides:
        inheritance_role = "override"
    elif implemented_methods:
        inheritance_role = "interface_implementation"
    elif row["visibility"] == "private":
        inheritance_role = "unknown"
    else:
        inheritance_role = "base_definition"

    return {
        "inheritance_role": inheritance_role,
        "overrides": overrides,
        "implements_method": implemented_methods,
        "parent_class": parent_class,
        "interface_names": interface_names,
        "parent_definition": parent_definition,
        "overrides_definition": overrides_definition,
        "implements_definitions": implements_definitions,
        "child_overrides": child_overrides,
    }


def _find_method_in_container(connection: sqlite3.Connection, container_name: str, method_name: str) -> sqlite3.Row | None:
    """Find a method by container name using exact matches before legacy fallbacks.

    Exact fully-qualified container matches must win over short-name matches so
    sibling classes such as ``aiprovider_openai\\provider`` and
    ``aiprovider_awsbedrock\\provider`` cannot be confused when the caller is
    explicitly walking a real extends/implements chain.
    """

    normalized = str(container_name).lstrip("\\")
    short_name = normalized.split("\\")[-1]
    rows = connection.execute(
        """
        SELECT
            s.*,
            f.repository_relative_path,
            f.moodle_path,
            c.name AS component_name
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        JOIN components c ON c.id = s.component_id
        WHERE s.symbol_type = 'method'
          AND s.name = ?
          AND (
                s.container_name = ?
             OR s.container_name = ?
             OR s.container_name LIKE ? ESCAPE '\\'
             OR s.container_name LIKE ? ESCAPE '\\'
          )
        ORDER BY s.fqname
        """,
        (
            method_name,
            normalized,
            f"\\{normalized}",
            f"%\\{short_name}",
            short_name,
        ),
    ).fetchall()
    ranked: list[tuple[tuple[int, str, int], sqlite3.Row]] = []
    for row in rows:
        container = _normalize_php_symbol_name(row["container_name"] or "")
        if container == normalized:
            rank = 0
        elif container.endswith(f"\\{normalized}"):
            rank = 1
        elif container == short_name:
            rank = 2
        elif container.endswith(f"\\{short_name}"):
            rank = 3
        else:
            continue
        ranked.append(((rank, row["fqname"], row["line"]), row))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1]


def _find_class_symbol(connection: sqlite3.Connection, class_name: str) -> sqlite3.Row | None:
    """Return the best class symbol match for a legacy or namespaced class name."""

    normalized = _normalize_php_symbol_name(class_name)
    short_name = normalized.split("\\")[-1]
    rows = connection.execute(
        """
        SELECT
            s.fqname,
            s.name,
            s.namespace,
            s.line,
            f.repository_relative_path,
            f.moodle_path,
            c.name AS component_name
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        JOIN components c ON c.id = s.component_id
        WHERE s.symbol_type IN ('class', 'interface', 'trait')
          AND (
                s.fqname = ?
             OR s.fqname = ?
             OR s.name = ?
             OR s.fqname LIKE ? ESCAPE '\\'
          )
        ORDER BY s.fqname, s.line
        """,
        (normalized, f"\\{normalized}", short_name, f"%\\{short_name}"),
    ).fetchall()
    ranked: list[tuple[tuple[int, str, int], sqlite3.Row]] = []
    for row in rows:
        fqname = _normalize_php_symbol_name(row["fqname"])
        name = _normalize_php_symbol_name(row["name"])
        if fqname == normalized:
            rank = 0
        elif name == normalized:
            rank = 1
        elif fqname.endswith(f"\\{normalized}"):
            rank = 2
        elif name == short_name:
            rank = 3
        else:
            continue
        ranked.append(((rank, fqname, row["line"]), row))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1]


def _find_inherited_method_definition(
    connection: sqlite3.Connection,
    class_symbol: sqlite3.Row,
    method_name: str,
) -> sqlite3.Row | None:
    """Return a parent/interface method when the queried class does not override it."""

    class_fqname = _normalize_php_symbol_name(class_symbol["fqname"])
    visited = {class_fqname}
    queue = [class_fqname]
    while queue:
        current = queue.pop(0)
        relationships = connection.execute(
            """
            SELECT relationship_type, target_name
            FROM relationships
            WHERE source_fqname = ?
              AND relationship_type IN ('extends', 'implements')
            ORDER BY CASE relationship_type WHEN 'extends' THEN 0 ELSE 1 END, target_name
            """,
            (current,),
        ).fetchall()
        for relation in relationships:
            target = _normalize_php_symbol_name(relation["target_name"])
            if not target or target in visited:
                continue
            visited.add(target)
            method_row = _find_method_in_container(connection, target, method_name)
            if method_row is not None:
                return method_row
            queue.append(target)
    return None


def _serialize_related_definition(row: sqlite3.Row) -> dict[str, object]:
    """Return a compact linked-definition payload."""

    return {
        "fqname": row["fqname"],
        "name": row["name"],
        "symbol_type": row["symbol_type"],
        "class_name": row["container_name"],
        "component": row["component_name"],
        "file": row["moodle_path"],
        "repository_relative_path": row["repository_relative_path"],
        "line": row["line"],
    }


def _find_child_override_definitions(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    limit: int,
) -> list[dict[str, object]]:
    """Return a bounded set of child methods overriding or implementing this method."""

    container_name = row["container_name"]
    if not container_name:
        return []
    relationship_types = ("extends", "implements") if row["symbol_type"] == "method" else ("extends",)
    pending = [container_name]
    visited = {container_name}
    descendants: list[str] = []
    while pending and len(descendants) < limit * 3:
        current = pending.pop(0)
        normalized_current = _normalize_php_symbol_name(current)
        short_current = normalized_current.split("\\")[-1]
        placeholders = ",".join("?" for _ in relationship_types)
        rows = connection.execute(
            f"""
            SELECT source_fqname
            FROM relationships
            WHERE target_name IN (?, ?, ?)
              AND relationship_type IN ({placeholders})
            ORDER BY source_fqname
            """,
            (current, normalized_current, short_current, *relationship_types),
        ).fetchall()
        for item in rows:
            source = item["source_fqname"]
            if source in visited:
                continue
            visited.add(source)
            descendants.append(source)
            pending.append(source)

    results: list[dict[str, object]] = []
    seen: set[str] = set()
    for descendant in descendants:
        method_row = _find_method_in_container(connection, descendant, row["name"])
        if method_row is None or method_row["fqname"] == row["fqname"] or method_row["fqname"] in seen:
            continue
        seen.add(method_row["fqname"])
        results.append(_serialize_related_definition(method_row))
        if len(results) >= limit:
            break
    return results


def _find_usage_examples(connection: sqlite3.Connection, row: sqlite3.Row, limit: int) -> list[dict[str, object]]:
    """Return a bounded set of higher-confidence usage examples for a symbol."""

    file_rows = connection.execute(
        """
        SELECT id, moodle_path, absolute_path, extension, file_role
        FROM files
        WHERE extension = '.php'
        ORDER BY moodle_path
        """
    ).fetchall()

    if row["symbol_type"] == "function":
        return _find_function_usage_examples(row, file_rows, limit)
    if row["symbol_type"] == "method":
        return _find_method_usage_examples(connection, row, file_rows, limit)
    if row["symbol_type"] == "class":
        return _find_class_usage_examples(row, file_rows, limit)
    return []


def _find_function_usage_examples(row: sqlite3.Row, file_rows: list[sqlite3.Row], limit: int) -> list[dict[str, object]]:
    """Return bounded function-call examples using exact call matching."""

    examples: list[tuple[int, dict[str, object]]] = []
    for file_row in file_rows:
        absolute_path = Path(file_row["absolute_path"])
        try:
            source = absolute_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line_number, line in enumerate(source.splitlines(), start=1):
            if file_row["id"] == row["file_id"] and line_number == row["line"]:
                continue
            if not _function_call_matches(str(row["name"]), line):
                continue
            usage_kind = _classify_usage_kind(file_row, "function_call")
            score = _usage_score(file_row, usage_kind, "high")
            examples.append(
                (
                    score,
                    {
                        "file": file_row["moodle_path"],
                        "line": line_number,
                        "usage_kind": usage_kind,
                        "confidence": "high",
                        "snippet": line.strip(),
                    },
                )
            )
    return _sorted_usage_examples(examples, limit)


def _find_class_usage_examples(row: sqlite3.Row, file_rows: list[sqlite3.Row], limit: int) -> list[dict[str, object]]:
    """Return bounded class-reference examples."""

    examples: list[tuple[int, dict[str, object]]] = []
    for file_row in file_rows:
        absolute_path = Path(file_row["absolute_path"])
        try:
            source = absolute_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line_number, line in enumerate(source.splitlines(), start=1):
            if file_row["id"] == row["file_id"] and line_number == row["line"]:
                continue
            usage_kind = _class_usage_kind_for_line(row, line)
            if usage_kind is None:
                continue
            classified_kind = _classify_usage_kind(file_row, usage_kind)
            score = _usage_score(file_row, classified_kind, "high")
            examples.append(
                (
                    score,
                    {
                        "file": file_row["moodle_path"],
                        "line": line_number,
                        "usage_kind": classified_kind,
                        "confidence": "high",
                        "snippet": line.strip(),
                    },
                )
            )
    return _sorted_usage_examples(examples, limit)


def _find_method_usage_examples(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    file_rows: list[sqlite3.Row],
    limit: int,
) -> list[dict[str, object]]:
    """Return bounded method usage examples using class-aware matching."""

    examples: list[tuple[int, dict[str, object]]] = []
    seen: set[tuple[str, int, str]] = set()

    if row["is_static"]:
        for item in _find_service_definition_examples(connection, row):
            key = (str(item["file"]), int(item["line"]), str(item["usage_kind"]))
            if key in seen:
                continue
            seen.add(key)
            examples.append((_usage_score({"moodle_path": item["file"], "file_role": None}, item["usage_kind"], item["confidence"]), item))

    for file_row in file_rows:
        absolute_path = Path(file_row["absolute_path"])
        try:
            source = absolute_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for item in _scan_method_usage_examples_in_source(row, file_row, source):
            if file_row["id"] == row["file_id"] and item["line"] == row["line"]:
                continue
            key = (str(item["file"]), int(item["line"]), str(item["usage_kind"]))
            if key in seen:
                continue
            seen.add(key)
            examples.append((_usage_score(file_row, item["usage_kind"], item["confidence"]), item))
    return _sorted_usage_examples(examples, limit)


def _function_call_matches(function_name: str, line: str) -> bool:
    """Return whether a line contains a likely function call."""

    pattern = re.compile(rf"(?<!function\s)\b{re.escape(function_name)}\s*\(")
    return pattern.search(line) is not None


def _class_usage_kind_for_line(row: sqlite3.Row, line: str) -> str | None:
    """Return a best-effort class usage kind for a single source line."""

    class_name = re.escape(str(row["name"]))
    if re.search(rf"\bnew\s+{class_name}\s*\(", line):
        return "class_instantiation"
    if re.search(rf"\bextends\s+{class_name}\b", line):
        return "extends_reference"
    if re.search(rf"\bimplements\b.*\b{class_name}\b", line):
        return "implements_reference"
    return None


def _find_service_definition_examples(connection: sqlite3.Connection, row: sqlite3.Row) -> list[dict[str, object]]:
    """Return high-confidence service-definition linkages for external execute methods."""

    if row["name"] != "execute" or not row["container_name"]:
        return []
    service_rows = connection.execute(
        """
        SELECT ws.service_name, ws.line, f.moodle_path
        FROM webservices ws
        JOIN files f ON f.id = ws.file_id
        WHERE ws.classname = ?
        ORDER BY f.moodle_path, ws.line, ws.service_name
        """,
        (str(row["container_name"]).lstrip("\\"),),
    ).fetchall()
    return [
        {
            "file": item["moodle_path"],
            "line": item["line"],
            "usage_kind": "service_definition",
            "confidence": "high",
            "snippet": item["service_name"],
        }
        for item in service_rows
    ]


def _scan_method_usage_examples_in_source(row: sqlite3.Row, file_row: sqlite3.Row, source: str) -> list[dict[str, object]]:
    """Return high-confidence method usage examples found in one PHP source file."""

    method_name = str(row["name"])
    class_candidates = _candidate_class_names(row["container_name"])
    examples: list[dict[str, object]] = []
    lines = source.splitlines()
    variable_types: dict[str, str] = {}

    for line_number, line in enumerate(lines, start=1):
        if re.search(r"^\s*(?:public|protected|private|final|abstract|static\s+)*function\b|^\s*function\b", line):
            variable_types = _typed_parameters_for_line(line)

        if row["is_static"]:
            if _static_call_matches(line, class_candidates, method_name):
                usage_kind = _classify_usage_kind(file_row, "static_method_call")
                examples.append(
                    {
                        "file": file_row["moodle_path"],
                        "line": line_number,
                        "usage_kind": usage_kind,
                        "confidence": "high",
                        "snippet": line.strip(),
                    }
                )
            continue

        direct_call = _direct_new_call_matches(line, class_candidates, method_name)
        if direct_call:
            usage_kind = _classify_usage_kind(file_row, "instance_method_call")
            examples.append(
                {
                    "file": file_row["moodle_path"],
                    "line": line_number,
                    "usage_kind": usage_kind,
                    "confidence": "high",
                    "snippet": line.strip(),
                }
            )
            continue

        assignment = _new_assignment_type(line, class_candidates)
        if assignment is not None:
            variable_types[assignment[0]] = assignment[1]

        variable_name = _instance_call_variable(line, method_name)
        if variable_name and variable_types.get(variable_name) in class_candidates:
            usage_kind = _classify_usage_kind(file_row, "instance_method_call")
            examples.append(
                {
                    "file": file_row["moodle_path"],
                    "line": line_number,
                    "usage_kind": usage_kind,
                    "confidence": "high",
                    "snippet": line.strip(),
                }
            )

    return examples


def _candidate_class_names(container_name: str | None) -> set[str]:
    """Return exact class-name variants for a container."""

    if not container_name:
        return set()
    normalized = str(container_name).lstrip("\\")
    short_name = normalized.split("\\")[-1]
    return {normalized, f"\\{normalized}", short_name}


def _static_call_matches(line: str, class_candidates: set[str], method_name: str) -> bool:
    """Return whether a line contains a high-confidence static method call."""

    match = re.search(rf"(?P<class>[\\A-Za-z_][\\A-Za-z0-9_]*)\s*::\s*{re.escape(method_name)}\s*\(", line)
    if match is None:
        return False
    class_name = match.group("class")
    return class_name in class_candidates or class_name.lstrip("\\") in class_candidates


def _direct_new_call_matches(line: str, class_candidates: set[str], method_name: str) -> bool:
    """Return whether a line directly instantiates a class and calls the target method."""

    match = re.search(rf"new\s+(?P<class>[\\A-Za-z_][\\A-Za-z0-9_]*)\s*\(.*\)\s*->\s*{re.escape(method_name)}\s*\(", line)
    if match is None:
        return False
    class_name = match.group("class")
    return class_name in class_candidates or class_name.lstrip("\\") in class_candidates


def _new_assignment_type(line: str, class_candidates: set[str]) -> tuple[str, str] | None:
    """Return a variable/class pair for a direct ``$var = new ClassName(...)`` assignment."""

    assignment_match = re.search(r"(?P<var>\$[A-Za-z_][A-Za-z0-9_]*)\s*=\s*new\s+(?P<class>[\\A-Za-z_][\\A-Za-z0-9_]*)\s*\(", line)
    if assignment_match is None:
        return None
    class_name = assignment_match.group("class")
    if class_name in class_candidates or class_name.lstrip("\\") in class_candidates:
        return assignment_match.group("var"), class_name if class_name in class_candidates else class_name.lstrip("\\")
    return None


def _instance_call_variable(line: str, method_name: str) -> str | None:
    """Return the variable name for a direct ``$var->method()`` call."""

    match = re.search(rf"(?P<var>\$[A-Za-z_][A-Za-z0-9_]*)\s*->\s*{re.escape(method_name)}\s*\(", line)
    return match.group("var") if match is not None else None


def _typed_parameters_for_line(line: str) -> dict[str, str]:
    """Return simple typed-parameter hints from a one-line function declaration."""

    if "(" not in line or ")" not in line:
        return {}
    raw_params = line.split("(", 1)[1].rsplit(")", 1)[0]
    typed_parameters: dict[str, str] = {}
    for part in raw_params.split(","):
        match = re.search(r"(?P<type>[\\A-Za-z_][\\A-Za-z0-9_]*|\?[\\A-Za-z_][\\A-Za-z0-9_]*)\s+(?P<var>\$[A-Za-z_][A-Za-z0-9_]*)", part.strip())
        if match is None:
            continue
        typed_parameters[match.group("var")] = match.group("type").lstrip("?")
    return typed_parameters


def _classify_usage_kind(file_row: sqlite3.Row | dict[str, object], base_kind: str) -> str:
    """Return a more expressive usage kind for a file-context/call combination."""

    moodle_path = str(file_row["moodle_path"])
    file_role = file_row["file_role"]
    if base_kind == "service_definition":
        return "service_definition"
    if "/tests/" in moodle_path:
        return "test_usage"
    if file_role == "renderer_file":
        return "renderer_usage"
    if "/classes/form/" in moodle_path or moodle_path.endswith("_form.php"):
        return "form_usage"
    return base_kind


def _usage_score(file_row: sqlite3.Row | dict[str, object], usage_kind: str, confidence: str) -> int:
    """Return a stable ranking score for usage examples."""

    moodle_path = str(file_row["moodle_path"])
    score = {
        "service_definition": 100,
        "renderer_usage": 92,
        "form_usage": 90,
        "static_method_call": 88,
        "instance_method_call": 85,
        "function_call": 82,
        "test_usage": 76,
        "class_instantiation": 70,
        "extends_reference": 66,
        "implements_reference": 66,
    }.get(usage_kind, 50)
    if moodle_path.endswith("/view.php") or moodle_path.endswith("/index.php"):
        score += 4
    if confidence == "medium":
        score -= 10
    elif confidence == "low":
        score -= 25
    return score


def _sorted_usage_examples(examples: list[tuple[int, dict[str, object]]], limit: int) -> list[dict[str, object]]:
    """Return usage examples sorted by score, path, line, and kind."""

    examples.sort(
        key=lambda item: (
            -item[0],
            str(item[1]["file"]),
            int(item[1]["line"]),
            str(item[1]["usage_kind"]),
        )
    )
    return [item for _, item in examples[:limit]]


def _summarize_usage_examples(examples: list[dict[str, object]]) -> dict[str, int]:
    """Return a compact usage-kind count summary."""

    summary: dict[str, int] = {}
    for item in examples:
        kind = str(item["usage_kind"])
        summary[kind] = summary.get(kind, 0) + 1
    return dict(sorted(summary.items()))


def _resolve_file_row(
    connection: sqlite3.Connection,
    repository: sqlite3.Row,
    file_path: str,
) -> sqlite3.Row:
    """Resolve a CLI file argument into the indexed file row."""

    candidate = Path(file_path).expanduser()
    if not candidate.is_absolute():
        lookup = normalize_relative_lookup_path(file_path)
        row = connection.execute(
            """
            SELECT
                f.id,
                f.repository_relative_path,
                f.moodle_path,
                f.path_scope,
                f.absolute_path,
                f.file_role,
                f.extension,
                c.name AS component_name
            FROM files f
            JOIN components c ON c.id = f.component_id
            WHERE f.moodle_path = ?
            ORDER BY CASE WHEN f.path_scope = 'application' THEN 0 ELSE 1 END, f.repository_relative_path
            LIMIT 1
            """,
            (lookup,),
        ).fetchone()
        if row is not None:
            return row

        row = connection.execute(
            """
            SELECT
                f.id,
                f.repository_relative_path,
                f.moodle_path,
                f.path_scope,
                f.absolute_path,
                f.file_role,
                f.extension,
                c.name AS component_name
            FROM files f
            JOIN components c ON c.id = f.component_id
            WHERE f.repository_relative_path = ?
            LIMIT 1
            """,
            (lookup,),
        ).fetchone()
        if row is not None:
            return row
        raise ValidationError(f"File not found in index: {lookup}")

    resolved = candidate.resolve()
    repository_root = Path(repository["repository_root"]).resolve()
    try:
        repository_relative_path = resolved.relative_to(repository_root).as_posix()
    except ValueError as exc:
        raise ValidationError(f"File path is outside the indexed repository: {resolved}") from exc

    row = connection.execute(
        """
        SELECT
            f.id,
            f.repository_relative_path,
            f.moodle_path,
            f.path_scope,
            f.absolute_path,
            f.file_role,
            f.extension,
            c.name AS component_name
        FROM files f
        JOIN components c ON c.id = f.component_id
        WHERE f.repository_relative_path = ?
        LIMIT 1
        """,
        (repository_relative_path,),
    ).fetchone()
    if row is None:
        raise ValidationError(f"File not found in index: {repository_relative_path}")
    return row


def _get_indexed_repository_metadata(connection: sqlite3.Connection) -> sqlite3.Row:
    """Return repository metadata recorded in the SQLite index."""

    row = connection.execute(
        """
        SELECT input_path, repository_root, application_root, layout_type
        FROM repositories
        ORDER BY id LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise ValidationError("Indexed repository metadata not found in database.")
    return row


def _service_related_suggestions(webservices: list[sqlite3.Row]) -> list[dict[str, str]]:
    """Return non-index-aware related suggestions from resolved service targets."""

    suggestions: list[dict[str, str]] = []
    for item in webservices:
        if not item["resolved_target_file"]:
            continue
        if item["resolution_type"] == "classpath":
            reason = f"suggested because db/services.php references this file via classpath for {item['service_name']}"
        else:
            reason = (
                f"suggested because db/services.php references class {item['classname']}, "
                f"which resolves to this file"
            )
        suggestions.append({"path": item["resolved_target_file"], "reason": reason})
    return suggestions


def _linked_class_artifacts(
    connection: sqlite3.Connection,
    relationships: list[sqlite3.Row],
) -> list[dict[str, object]]:
    """Return resolved class/file artifacts for one file.

    This helper keeps Phase 1 resolution deterministic:
    - Moodle autoloaded namespaced classes resolve via frankenstyle component
      namespaces.
    - a short explicit map handles important legacy framework classes such as
      ``moodleform``.
    - output classes additionally surface likely paired Mustache templates.
    """

    artifacts: list[dict[str, object]] = []
    seen_relationships: set[tuple[str, str]] = set()
    for item in relationships:
        if item["relationship_type"] not in {"references_class", "extends"}:
            continue
        class_name = str(item["target_name"]).lstrip("\\")
        relationship_key = (item["relationship_type"], class_name)
        if relationship_key in seen_relationships:
            continue
        seen_relationships.add(relationship_key)
        target_file = resolve_classname_to_file_path(class_name)
        if target_file is None:
            target_file = resolve_framework_class_to_file_path(class_name)
        if target_file is None:
            continue
        file_exists = connection.execute(
            "SELECT 1 FROM files WHERE moodle_path = ? OR repository_relative_path = ?",
            (target_file, target_file),
        ).fetchone()
        template_files = _existing_template_candidates(connection, target_file)
        artifact_kind = _class_artifact_kind(target_file)
        artifacts.append(
            {
                "class_name": class_name,
                "relationship_type": item["relationship_type"],
                "resolved_target_file": target_file,
                "resolved": bool(file_exists),
                "artifact_kind": artifact_kind,
                "template_files": template_files,
            }
        )
    return artifacts


def _class_related_suggestions(class_references: list[dict[str, object]]) -> list[dict[str, str]]:
    """Return file-context related suggestions for resolved class artifacts."""

    suggestions: list[dict[str, str]] = []
    for item in class_references:
        suggestions.append(
            {
                "path": str(item["resolved_target_file"]),
                "reason": _class_artifact_reason(item),
            }
        )
        for template_path in item["template_files"]:
            suggestions.append(
                {
                    "path": template_path,
                    "reason": (
                        f"suggested because output class \\{item['class_name']} likely renders "
                        "through this Mustache template"
                    ),
                }
            )
    return suggestions


def _service_test_suggestions(
    connection: sqlite3.Connection,
    webservices: list[sqlite3.Row],
) -> list[dict[str, str]]:
    """Return file-context related suggestions for service-linked tests."""

    suggestions: list[dict[str, str]] = []
    for candidate in _service_test_candidates(webservices):
        exists = connection.execute(
            "SELECT 1 FROM files WHERE moodle_path = ? OR repository_relative_path = ?",
            (candidate["path"], candidate["path"]),
        ).fetchone()
        if exists:
            suggestions.append(
                {
                    "path": candidate["path"],
                    "reason": candidate["reason"],
                }
            )
    return suggestions


def _indexed_service_suggestions(
    connection: sqlite3.Connection,
    webservices: list[sqlite3.Row],
) -> list[dict[str, object]]:
    """Return indexed related-file suggestions from resolved service targets."""

    target_counts: dict[str, int] = {}
    target_reasons: dict[str, str] = {}
    for item in webservices:
        target_file = item["resolved_target_file"]
        if not target_file:
            continue
        target_counts[target_file] = target_counts.get(target_file, 0) + 1
        if target_file not in target_reasons:
            if item["resolution_type"] == "classpath":
                target_reasons[target_file] = (
                    f"suggested because db/services.php references this file via classpath for {item['service_name']}"
                )
            else:
                target_reasons[target_file] = (
                    f"suggested because db/services.php references class {item['classname']}, "
                    "which resolves to this file"
                )

    suggestions: list[dict[str, object]] = []
    for target_file, count in sorted(target_counts.items()):
        exists = connection.execute(
            "SELECT 1 FROM files WHERE moodle_path = ? OR repository_relative_path = ?",
            (target_file, target_file),
        ).fetchone()
        reason = target_reasons[target_file]
        if count > 1:
            reason = f"{reason}; multiple services in this file resolve here"
        suggestions.append(
            {
                "path": target_file,
                "reason": reason,
                "indexed": bool(exists),
            }
        )
    return suggestions


def _indexed_service_test_suggestions(
    connection: sqlite3.Connection,
    webservices: list[sqlite3.Row],
) -> list[dict[str, object]]:
    """Return suggest-related entries for service-linked tests."""

    suggestions: list[dict[str, object]] = []
    for candidate in _service_test_candidates(webservices):
        exists = connection.execute(
            "SELECT 1 FROM files WHERE moodle_path = ? OR repository_relative_path = ?",
            (candidate["path"], candidate["path"]),
        ).fetchone()
        if not exists:
            continue
        suggestions.append(
            {
                "path": candidate["path"],
                "reason": candidate["reason"],
                "indexed": True,
            }
        )
    return suggestions


def _indexed_class_suggestions(
    connection: sqlite3.Connection,
    relationships: list[sqlite3.Row],
) -> list[dict[str, object]]:
    """Return suggest-related entries for resolved class/file companions."""

    artifacts = _linked_class_artifacts(connection, relationships)
    suggestions: list[dict[str, object]] = []
    for item in artifacts:
        target_path = str(item["resolved_target_file"])
        if item["resolved"]:
            suggestions.append(
                {
                    "path": target_path,
                    "reason": _class_artifact_reason(item),
                    "indexed": True,
                }
            )
        for template_path in item["template_files"]:
            suggestions.append(
                {
                    "path": template_path,
                    "reason": (
                        f"suggested because output class \\{item['class_name']} likely renders "
                        "through this Mustache template"
                    ),
                    "indexed": True,
                }
            )
    return suggestions


def _linked_service_tests(
    connection: sqlite3.Connection,
    webservices: list[sqlite3.Row],
) -> list[dict[str, object]]:
    """Return likely related test files for a services definition file."""

    linked: list[dict[str, object]] = []
    for candidate in _service_test_candidates(webservices):
        file_row = connection.execute(
            """
            SELECT moodle_path, repository_relative_path, file_role
            FROM files
            WHERE (moodle_path = ? OR repository_relative_path = ?)
              AND file_role = 'phpunit_test'
            LIMIT 1
            """,
            (candidate["path"], candidate["path"]),
        ).fetchone()
        if file_row is None:
            continue
        linked.append(
            {
                "name": Path(file_row["moodle_path"]).name,
                "test_type": file_row["file_role"],
                "line": None,
                "file": file_row["moodle_path"],
                "reason": candidate["reason"],
            }
        )
    return _deduplicate_tests(linked)


def _service_test_candidates(webservices: list[sqlite3.Row]) -> list[dict[str, str]]:
    """Return deterministic test-file candidates for resolved service targets."""

    candidates: list[dict[str, str]] = []
    for item in webservices:
        target_file = item["resolved_target_file"]
        if not target_file:
            continue

        if target_file.endswith("/externallib.php"):
            component_root = target_file.removesuffix("/externallib.php")
            candidates.append(
                {
                    "path": f"{component_root}/tests/externallib_test.php",
                    "reason": "suggested because externallib.php changes are often covered by externallib_test.php",
                }
            )
            candidates.append(
                {
                    "path": f"{component_root}/tests/externallib_advanced_testcase.php",
                    "reason": (
                        "suggested because externallib_advanced_testcase.php appears to provide "
                        "shared web service test coverage for externallib-based services"
                    ),
                }
            )
            continue

        if "/classes/external/" in target_file:
            component_root, class_suffix = target_file.split("/classes/external/", 1)
            class_name = class_suffix.removesuffix(".php")
            candidates.append(
                {
                    "path": f"{component_root}/tests/external/{class_name}_test.php",
                    "reason": (
                        f"suggested because service class {item['classname']} is typically covered "
                        "by this PHPUnit test file"
                    ),
                }
            )

    return candidates


def _deduplicate_suggestions(suggestions: list[dict[str, str]]) -> list[dict[str, str]]:
    """Deduplicate non-index-aware suggestions by path, merging distinct reasons."""

    merged: dict[str, dict[str, str]] = {}
    for item in suggestions:
        existing = merged.get(item["path"])
        if existing is None:
            merged[item["path"]] = dict(item)
            continue
        if item["reason"] not in existing["reason"]:
            existing["reason"] = f"{existing['reason']} | {item['reason']}"
    return [merged[path] for path in sorted(merged)]


def _deduplicate_indexed_suggestions(suggestions: list[dict[str, object]]) -> list[dict[str, object]]:
    """Deduplicate suggest-related results by path, preserving indexed truthiness."""

    merged: dict[str, dict[str, object]] = {}
    for item in suggestions:
        existing = merged.get(item["path"])
        if existing is None:
            merged[item["path"]] = dict(item)
            continue
        existing["indexed"] = bool(existing["indexed"] or item["indexed"])
        if item["reason"] not in str(existing["reason"]):
            existing["reason"] = f"{existing['reason']} | {item['reason']}"
    return [merged[path] for path in sorted(merged)]


def _deduplicate_tests(tests: list[dict[str, object]]) -> list[dict[str, object]]:
    """Deduplicate linked test entries by file path."""

    seen: set[str] = set()
    ordered: list[dict[str, object]] = []
    for item in tests:
        if item["file"] in seen:
            continue
        seen.add(str(item["file"]))
        ordered.append(item)
    return ordered


def _existing_template_candidates(connection: sqlite3.Connection, output_file: str) -> list[str]:
    """Return indexed Mustache templates that plausibly pair with one output class."""

    if "/classes/output/" not in output_file:
        return []
    component_root, class_suffix = output_file.split("/classes/output/", 1)
    template_path = f"{component_root}/templates/{class_suffix.removesuffix('.php')}.mustache"
    row = connection.execute(
        "SELECT 1 FROM files WHERE moodle_path = ? OR repository_relative_path = ?",
        (template_path, template_path),
    ).fetchone()
    return [template_path] if row else []


def _class_artifact_kind(target_file: str) -> str:
    """Return a coarse artifact kind for a resolved class target file."""

    if target_file == "lib/formslib.php":
        return "framework_base"
    if "/classes/output/" in target_file:
        return "output_class"
    if "/classes/form/" in target_file:
        return "form_class"
    return "class_file"


def _class_artifact_reason(item: dict[str, object]) -> str:
    """Return a human-readable explanation for a resolved class artifact."""

    class_name = str(item["class_name"])
    relationship_type = str(item["relationship_type"])
    artifact_kind = str(item["artifact_kind"])

    if relationship_type == "extends" and artifact_kind == "framework_base":
        return (
            f"suggested because this class extends {class_name}, whose core base "
            "implementation lives in this file"
        )
    if relationship_type == "extends":
        return f"suggested because this class extends {class_name}, which is implemented in this file"
    if artifact_kind == "output_class":
        return f"suggested because this file references class \\{class_name}, which resolves to this output file"
    if artifact_kind == "form_class":
        return f"suggested because this file references class \\{class_name}, which resolves to this form class file"
    if artifact_kind == "framework_base":
        return f"suggested because this file references core framework class \\{class_name}, which is defined here"
    return f"suggested because this file references class \\{class_name}, which resolves to this file"


def _serialize_js_module(js_module: sqlite3.Row | None, connection: sqlite3.Connection) -> dict[str, object] | None:
    """Return a JSON-friendly JS module payload for ``file-context``."""

    if js_module is None:
        return None
    superclass_resolution = (
        resolve_js_module(connection, js_module["superclass_module"])
        if js_module["superclass_module"]
        else None
    )
    build_indexed = False
    build_file = js_module["build_file"]
    if build_file:
        build_indexed = bool(
            connection.execute(
                "SELECT 1 FROM files WHERE moodle_path = ? OR repository_relative_path = ?",
                (build_file, build_file),
            ).fetchone()
        )
    return {
        "module_name": js_module["module_name"],
        "export_kind": js_module["export_kind"],
        "export_name": js_module["export_name"],
        "superclass_name": js_module["superclass_name"],
        "superclass_module": js_module["superclass_module"],
        "resolved_superclass_file": (
            superclass_resolution.source_file if superclass_resolution else js_module["resolved_superclass_file"]
        ),
        "superclass_resolution_strategy": (
            superclass_resolution.resolution_strategy if superclass_resolution else None
        ),
        "build_file": build_file,
        "build_indexed": build_indexed,
        "build_status": js_module["build_status"],
    }


def _serialize_js_import(connection: sqlite3.Connection, js_import: sqlite3.Row) -> dict[str, object]:
    """Return a JSON-friendly JS import payload using registry-first resolution."""

    resolution = resolve_js_module(connection, js_import["module_name"])
    return {
        "module_name": js_import["module_name"],
        "line": js_import["line"],
        "import_kind": js_import["import_kind"],
        "imported_name": js_import["imported_name"],
        "local_name": js_import["local_name"],
        "resolved_target_file": resolution.source_file,
        "build_file": resolution.build_file,
        "resolution_status": resolution.resolution_status,
        "resolution_strategy": resolution.resolution_strategy,
        "is_external": resolution.is_external,
    }


def _js_related_suggestions(
    connection: sqlite3.Connection,
    js_module: sqlite3.Row | None,
    js_imports: list[dict[str, object]],
) -> list[dict[str, str]]:
    """Return ``file-context`` related suggestions for indexed JS module metadata."""

    suggestions: list[dict[str, str]] = []
    for item in js_imports:
        if item["resolved_target_file"]:
            suggestions.append(
                {
                    "path": item["resolved_target_file"],
                    "reason": f"suggested because this source file imports {item['module_name']}",
                }
            )
    if js_module is not None and js_module["superclass_module"]:
        superclass_resolution = resolve_js_module(connection, js_module["superclass_module"])
        if superclass_resolution.source_file:
            suggestions.append(
                {
                    "path": superclass_resolution.source_file,
                    "reason": (
                        f"suggested because this source file extends {js_module['superclass_name']} "
                        f"from {js_module['superclass_module']}"
                    ),
                }
            )
    if js_module is not None and js_module["build_file"]:
        build_file = js_module["build_file"]
        build_exists = connection.execute(
            "SELECT 1 FROM files WHERE moodle_path = ? OR repository_relative_path = ?",
            (build_file, build_file),
        ).fetchone()
        if build_exists:
            suggestions.append(
                {
                    "path": build_file,
                    "reason": "suggested because this is the built artifact generated from the AMD source module",
                }
            )
    return suggestions


def _indexed_js_suggestions(
    connection: sqlite3.Connection,
    js_module: sqlite3.Row | None,
    js_imports: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return ``suggest-related`` entries for indexed JS module metadata."""

    suggestions: list[dict[str, object]] = []
    for item in js_imports:
        if not item["resolved_target_file"]:
            continue
        exists = connection.execute(
            "SELECT 1 FROM files WHERE moodle_path = ? OR repository_relative_path = ?",
            (item["resolved_target_file"], item["resolved_target_file"]),
        ).fetchone()
        suggestions.append(
            {
                "path": item["resolved_target_file"],
                "reason": f"suggested because this source file imports {item['module_name']}",
                "indexed": bool(exists),
            }
        )
    if js_module is not None and js_module["superclass_module"]:
        superclass_resolution = resolve_js_module(connection, js_module["superclass_module"])
        if superclass_resolution.source_file:
            exists = connection.execute(
                "SELECT 1 FROM files WHERE moodle_path = ? OR repository_relative_path = ?",
                (superclass_resolution.source_file, superclass_resolution.source_file),
            ).fetchone()
            suggestions.append(
                {
                    "path": superclass_resolution.source_file,
                    "reason": (
                        f"suggested because this source file extends {js_module['superclass_name']} "
                        f"from {js_module['superclass_module']}"
                    ),
                    "indexed": bool(exists),
                }
            )
    if js_module is not None and js_module["build_file"]:
        build_file = js_module["build_file"]
        exists = connection.execute(
            "SELECT 1 FROM files WHERE moodle_path = ? OR repository_relative_path = ?",
            (build_file, build_file),
        ).fetchone()
        if exists:
            suggestions.append(
                {
                    "path": build_file,
                    "reason": "suggested because this is the built artifact generated from the AMD source module",
                    "indexed": True,
                }
            )
    return suggestions

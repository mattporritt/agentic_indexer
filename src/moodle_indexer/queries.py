"""Query services over the SQLite index.

These functions implement the fixed Phase 1 CLI commands and keep SQL details
out of the command-line interface layer.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

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


@dataclass(slots=True)
class JsDefinitionCandidate:
    """A matched JavaScript module definition plus its indexed context."""

    row: sqlite3.Row
    matched_via: str = "direct_definition"


@dataclass(slots=True)
class SemanticChunk:
    """A deterministic structural chunk used by the Phase 4D hybrid retriever."""

    chunk_id: str
    path: str
    symbol: str | None
    component: str
    file_role: str
    language: str
    symbol_type: str | None
    line: int | None
    source_kind: str
    title: str
    summary: str
    text: str
    snippet: str | None = None


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

    if symbol_type == "js_module":
        matches = _find_js_module_definitions(connection, symbol_query, limit)
    elif "::" in symbol_query:
        matches = _find_method_definitions(connection, symbol_query, symbol_type, limit)
    else:
        matches = _find_named_definitions(connection, symbol_query, symbol_type, limit)
        if not matches and symbol_type == "any" and "/" in symbol_query:
            matches = _find_js_module_definitions(connection, symbol_query, limit)

    results = []
    for candidate in matches[:limit]:
        if isinstance(candidate, JsDefinitionCandidate):
            payload = _serialize_js_definition_match(connection, candidate)
            if include_usages:
                payload["usage_examples"] = _find_js_usage_examples(connection, candidate.row, limit=min(5, limit))
                payload["usage_summary"] = _summarize_usage_examples(payload["usage_examples"])
            else:
                payload["usage_examples"] = []
                payload["usage_summary"] = {}
        else:
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
            js_modules.id,
            js_modules.module_name,
            f.moodle_path,
            js_modules.export_kind,
            js_modules.export_name,
            js_modules.superclass_name,
            js_modules.superclass_module,
            js_modules.resolved_superclass_file,
            js_modules.build_file,
            js_modules.build_status
        FROM js_modules
        JOIN files f ON f.id = js_modules.file_id
        WHERE js_modules.file_id = ?
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
    service_artifacts = _build_service_linked_artifacts(
        connection,
        [
            {
                **dict(item),
                "source_file": moodle_path,
            }
            for item in webservices
        ],
    )
    rendering_artifacts = _build_rendering_linked_artifacts(connection, row, class_references)
    js_navigation = _build_js_navigation_artifacts(connection, js_module, js_imports)
    entrypoint_links = _build_entrypoint_links(
        connection,
        row,
        service_artifacts,
        rendering_artifacts,
        js_navigation,
    )

    related_suggestions = [
        {"path": item.path, "reason": item.reason}
        for item in suggest_related_files(moodle_path)
    ]
    related_suggestions.extend(_service_related_suggestions(webservices))
    related_suggestions.extend(_service_test_suggestions(connection, webservices))
    related_suggestions.extend(_class_related_suggestions(class_references))
    related_suggestions.extend(_js_related_suggestions(connection, js_module, js_imports))
    related_suggestions.extend(
        [
            {"path": item["path"], "reason": item["reason"]}
            for item in entrypoint_links
        ]
    )
    related_suggestions = _deduplicate_suggestions(
        _prune_generic_suggestions(related_suggestions),
        limit=20,
    )

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
        "linked_artifacts": {
            "services": service_artifacts,
            "rendering": rendering_artifacts,
            "javascript": js_navigation,
            "entrypoints": entrypoint_links,
        },
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
    js_modules = connection.execute(
        """
        SELECT jm.module_name, f.moodle_path, jm.build_file, jm.export_kind, jm.superclass_module
        FROM js_modules jm
        JOIN files f ON f.id = jm.file_id
        WHERE jm.component_id = ?
        ORDER BY jm.module_name
        LIMIT 20
        """,
        (component["id"],),
    ).fetchall()
    rendering_files = connection.execute(
        """
        SELECT moodle_path, file_role
        FROM files
        WHERE component_id = ?
          AND file_role IN ('renderer_file', 'output_class', 'template_file')
        ORDER BY file_role, moodle_path
        LIMIT 20
        """,
        (component["id"],),
    ).fetchall()
    entrypoints = connection.execute(
        """
        SELECT moodle_path, file_role
        FROM files
        WHERE component_id = ?
          AND file_role IN (
              'lib_file',
              'locallib_file',
              'settings_file',
              'services_definition',
              'renderer_file',
              'output_class',
              'template_file',
              'amd_source'
          )
        ORDER BY file_role, moodle_path
        LIMIT 20
        """,
        (component["id"],),
    ).fetchall()

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
        "js_modules": [dict(item) for item in js_modules],
        "tests": [dict(item) for item in tests],
        "sample_symbols": [dict(item) for item in symbols],
        "linked_artifacts": {
            "service_navigation": _build_service_linked_artifacts(
                connection,
                [
                    {
                        **dict(item),
                        "source_file": f"{component['root_path']}/db/services.php",
                    }
                    for item in webservices
                ],
            ),
            "rendering_files": [dict(item) for item in rendering_files],
            "entrypoints": [dict(item) for item in entrypoints],
        },
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
            js_modules.id,
            js_modules.module_name,
            f.moodle_path,
            js_modules.export_kind,
            js_modules.export_name,
            js_modules.superclass_name,
            js_modules.superclass_module,
            js_modules.resolved_superclass_file,
            js_modules.build_file,
            js_modules.build_status
        FROM js_modules
        JOIN files f ON f.id = js_modules.file_id
        WHERE js_modules.file_id = ?
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
    class_references = _linked_class_artifacts(connection, rendering_relationships)
    service_artifacts = _build_service_linked_artifacts(
        connection,
        [
            {
                **dict(item),
                "source_file": moodle_path,
            }
            for item in webservices
        ],
    )
    rendering_artifacts = _build_rendering_linked_artifacts(connection, row, class_references)
    js_navigation = _build_js_navigation_artifacts(connection, js_module, js_imports)
    entrypoint_links = _build_entrypoint_links(
        connection,
        row,
        service_artifacts,
        rendering_artifacts,
        js_navigation,
    )
    suggestions.extend(_indexed_js_suggestions(connection, js_module, js_imports))
    suggestions.extend(
        [
            {
                "path": item["path"],
                "reason": item["reason"],
                "indexed": bool(item["indexed"]),
            }
            for item in entrypoint_links
        ]
    )
    suggestions = _deduplicate_indexed_suggestions(
        _prune_generic_suggestions(suggestions),
        limit=20,
    )
    return {
        "file": moodle_path,
        "repository_relative_path": row["repository_relative_path"],
        "moodle_path": moodle_path,
        "linked_artifacts": {
            "services": service_artifacts,
            "rendering": rendering_artifacts,
            "javascript": js_navigation,
            "entrypoints": entrypoint_links,
        },
        "suggestions": suggestions,
    }


def find_related_definitions(
    connection: sqlite3.Connection,
    *,
    symbol_query: str | None = None,
    file_path: str | None = None,
    limit: int = 12,
) -> dict[str, object]:
    """Return bounded, high-confidence related definitions around a symbol or file.

    Phase 4A keeps this intentionally practical:
    - resolve the user's anchor symbol or file using existing query endpoints
    - reuse the already indexed inheritance/service/rendering/form/JS links
    - translate them into bounded primary/secondary definition-oriented items
    """

    if bool(symbol_query) == bool(file_path):
        raise ValidationError("Provide exactly one of --symbol or --file.")

    if symbol_query:
        definition_data = find_definition(connection, symbol_query, limit=max(1, min(limit, 5)), include_usages=False)
        matches = definition_data["matches"]
        items = _related_items_for_symbol_results(connection, matches)
        return {
            "query": symbol_query,
            "query_type": "symbol",
            "matched_definitions": [_compact_match_summary(item) for item in matches],
            "total_matches": definition_data["total_matches"],
            **_split_navigation_items(items, limit=limit),
        }

    context = file_context(connection, file_path or "")
    items = _related_items_for_file_context(context)
    return {
        "query": context["moodle_path"],
        "query_type": "file",
        "matched_definitions": [],
        "total_matches": 1,
        **_split_navigation_items(items, limit=limit),
    }


def suggest_edit_surface(
    connection: sqlite3.Connection,
    *,
    symbol_query: str | None = None,
    file_path: str | None = None,
    limit: int = 12,
) -> dict[str, object]:
    """Return the likely primary and secondary edit surface around a symbol or file."""

    if bool(symbol_query) == bool(file_path):
        raise ValidationError("Provide exactly one of --symbol or --file.")

    if symbol_query:
        definition_data = find_definition(connection, symbol_query, limit=max(1, min(limit, 5)), include_usages=False)
        matches = definition_data["matches"]
        if not matches:
            return {
                "query": symbol_query,
                "query_type": "symbol",
                "matched_definitions": [],
                "total_matches": 0,
                "primary_edit_surface": [],
                "secondary_edit_surface": [],
            }
        items = _edit_surface_items_for_symbol_results(connection, matches)
        return {
            "query": symbol_query,
            "query_type": "symbol",
            "matched_definitions": [_compact_match_summary(item) for item in matches],
            "total_matches": definition_data["total_matches"],
            **_split_navigation_items(items, limit=limit, primary_key="primary_edit_surface", secondary_key="secondary_edit_surface"),
        }

    context = file_context(connection, file_path or "")
    related = suggest_related(connection, file_path or "")
    items = _edit_surface_items_for_file_context(context, related)
    return {
        "query": context["moodle_path"],
        "query_type": "file",
        "matched_definitions": [],
        "total_matches": 1,
        **_split_navigation_items(items, limit=limit, primary_key="primary_edit_surface", secondary_key="secondary_edit_surface"),
    }


def dependency_neighborhood(
    connection: sqlite3.Connection,
    *,
    symbol_query: str | None = None,
    file_path: str | None = None,
    limit: int = 8,
) -> dict[str, object]:
    """Return a bounded dependency neighborhood around a symbol or file.

    Phase 4B stays intentionally local and high-confidence:
    - likely callers come from direct usage examples, service registrations, and
      direct JS importers where the index already has concrete evidence
    - likely callees come from direct service/rendering/form/JS links already
      extracted during indexing
    - linked tests and linked artifact companions are exposed as first-class
      sections so agents can inspect a small, practical implementation surface

    Phase 4C keeps the same relationships and depth, but upgrades the payload
    into a ranked, decision-ready view:
    - each section is summarized and its items are scored deterministically
    - the output surfaces a small cross-section ``primary_focus`` list so an
      agent can start with the most actionable files first
    - explanations and suggested actions are presentation-layer refinements on
      top of the existing trusted relationships rather than new analysis
    """

    if bool(symbol_query) == bool(file_path):
        raise ValidationError("Provide exactly one of --symbol or --file.")

    if symbol_query:
        definition_data = find_definition(connection, symbol_query, limit=max(1, min(limit, 4)), include_usages=True)
        matches = definition_data["matches"]
        payload = {
            "query": symbol_query,
            "query_kind": "symbol",
            "matched_definitions": [_compact_match_summary(item) for item in matches],
            "total_matches": definition_data["total_matches"],
        }
        payload.update(_dependency_sections_for_symbol_results(matches, limit=limit))
        return payload

    context = file_context(connection, file_path or "")
    payload = {
        "query": context["moodle_path"],
        "query_kind": "file",
        "matched_definitions": [],
        "total_matches": 1,
    }
    payload.update(_dependency_sections_for_file_context(context, limit=limit))
    return payload


def semantic_context(
    connection: sqlite3.Connection,
    *,
    symbol_query: str | None = None,
    file_path: str | None = None,
    query_text: str | None = None,
    limit: int = 10,
) -> dict[str, object]:
    """Return bounded hybrid semantic context around a symbol, file, or query.

    Phase 4D keeps structural navigation as the spine:
    - resolve the user's structural anchor first when a symbol or file is given
    - seed retrieval with the existing bounded dependency neighborhood
    - expand with lexical + hashed-vector similarity over deterministic chunks
    - rerank so direct structural context stays ahead of distant similar examples
    """

    targets = [bool(symbol_query), bool(file_path), bool(query_text)]
    if sum(targets) != 1:
        raise ValidationError("Provide exactly one of --symbol, --file, or --query.")

    bounded_limit = max(1, min(limit, 10))
    if symbol_query:
        return _semantic_context_for_symbol(connection, symbol_query, bounded_limit)
    if file_path:
        return _semantic_context_for_file(connection, file_path, bounded_limit)
    return _semantic_context_for_query(connection, query_text or "", bounded_limit)


def propose_change_plan(
    connection: sqlite3.Connection,
    *,
    symbol_query: str | None = None,
    file_path: str | None = None,
    query_text: str | None = None,
    limit: int = 10,
) -> dict[str, object]:
    """Return a bounded, agent-usable change plan around a symbol, file, or goal.

    Phase 4E keeps planning intentionally conservative:
    - reuse the existing structural and semantic endpoints as the substrate
    - classify the strongest local artifacts into required/likely/optional edits
    - derive a compact validation impact view from concrete tests/build surfaces
    - suggest a short recommended inspection/update sequence without executing it
    """

    targets = [bool(symbol_query), bool(file_path), bool(query_text)]
    if sum(targets) != 1:
        raise ValidationError("Provide exactly one of --symbol, --file, or --query.")

    bounded_limit = max(1, min(limit, 10))
    if symbol_query:
        return _change_plan_for_symbol(connection, symbol_query, bounded_limit)
    if file_path:
        return _change_plan_for_file(connection, file_path, bounded_limit)
    return _change_plan_for_query(connection, query_text or "", bounded_limit)


def _change_plan_for_symbol(
    connection: sqlite3.Connection,
    symbol_query: str,
    limit: int,
) -> dict[str, object]:
    """Return a change plan anchored on one resolved symbol."""

    definition_data = find_definition(connection, symbol_query, limit=max(1, min(limit, 4)), include_usages=True)
    matches = definition_data["matches"]
    if not matches:
        return _empty_change_plan(symbol_query, "symbol")

    anchor_match = matches[0]
    profile = _plan_profile_for_symbol(anchor_match)
    edit_surface = suggest_edit_surface(connection, symbol_query=symbol_query, limit=limit)
    neighborhood = dependency_neighborhood(connection, symbol_query=symbol_query, limit=min(6, limit))
    semantic = semantic_context(connection, symbol_query=symbol_query, limit=min(6, limit))
    plan = _synthesize_change_plan(
        query=symbol_query,
        query_kind="symbol",
        anchor=_compact_match_summary(anchor_match),
        matched_definitions=[_compact_match_summary(item) for item in matches],
        total_matches=definition_data["total_matches"],
        profile=profile,
        edit_surface=edit_surface,
        neighborhood=neighborhood,
        semantic=semantic,
        limit=limit,
    )
    return plan


def _change_plan_for_file(
    connection: sqlite3.Connection,
    file_path: str,
    limit: int,
) -> dict[str, object]:
    """Return a change plan anchored on one indexed file."""

    context = file_context(connection, file_path)
    profile = _plan_profile_for_file(context)
    edit_surface = suggest_edit_surface(connection, file_path=file_path, limit=limit)
    neighborhood = dependency_neighborhood(connection, file_path=file_path, limit=min(6, limit))
    semantic = semantic_context(connection, file_path=file_path, limit=min(6, limit))
    plan = _synthesize_change_plan(
        query=context["moodle_path"],
        query_kind="file",
        anchor={
            "path": context["moodle_path"],
            "file_role": context["file_role"],
            "component": context["component"],
        },
        matched_definitions=[],
        total_matches=1,
        profile=profile,
        edit_surface=edit_surface,
        neighborhood=neighborhood,
        semantic=semantic,
        limit=limit,
    )
    return plan


def _change_plan_for_query(
    connection: sqlite3.Connection,
    query_text: str,
    limit: int,
) -> dict[str, object]:
    """Return a conservative change plan for a free-text change goal."""

    semantic = semantic_context(connection, query_text=query_text, limit=min(6, limit))
    profile = _plan_profile_for_query(query_text)
    plan = _synthesize_change_plan(
        query=query_text,
        query_kind="query",
        anchor=None,
        matched_definitions=[],
        total_matches=int(semantic.get("total_matches", 0) or 0),
        profile=profile,
        edit_surface=None,
        neighborhood=None,
        semantic=semantic,
        limit=limit,
    )
    return plan


def _empty_change_plan(query: str, query_kind: str) -> dict[str, object]:
    """Return an empty change-plan payload."""

    return {
        "query": query,
        "query_kind": query_kind,
        "anchor": None,
        "matched_definitions": [],
        "total_matches": 0,
        "required_edits": [],
        "likely_edits": [],
        "optional_edits": [],
        "validation_impact": [],
        "recommended_sequence": [],
        "notes": [
            "No matching anchor was found, so the planner could not synthesize a bounded edit set.",
        ],
    }


def _synthesize_change_plan(
    *,
    query: str,
    query_kind: str,
    anchor: dict[str, object] | None,
    matched_definitions: list[dict[str, object]],
    total_matches: int,
    profile: dict[str, object],
    edit_surface: dict[str, object] | None,
    neighborhood: dict[str, object] | None,
    semantic: dict[str, object] | None,
    limit: int,
) -> dict[str, object]:
    """Combine trusted structural and semantic signals into one bounded plan."""

    candidates: list[dict[str, object]] = []
    anchor_path = str(profile.get("anchor_path") or "")
    anchor_symbol = str(profile.get("anchor_symbol") or "") or None
    if anchor_path:
        candidates.append(
            _change_plan_candidate(
                path=anchor_path,
                symbol=anchor_symbol,
                confidence="high",
                reason=_anchor_change_reason(profile),
                profile=profile,
                relationship="anchor_definition",
                item_type="definition_file" if query_kind == "symbol" else "anchor_file",
                source_rank=0,
            )
        )

    if edit_surface:
        for item in edit_surface.get("primary_edit_surface", []):
            candidate = _change_plan_candidate(
                path=str(item["path"]),
                symbol=_none_if_empty(item.get("symbol")),
                confidence=str(item.get("confidence") or "medium"),
                reason=str(item.get("reason") or ""),
                profile=profile,
                relationship=str(item.get("relationship") or "edit_surface"),
                item_type=str(item.get("type") or _artifact_item_type(str(item["path"]), None)),
                source_rank=1,
            )
            if candidate is not None:
                candidates.append(candidate)
        for item in edit_surface.get("secondary_edit_surface", []):
            candidate = _change_plan_candidate(
                path=str(item["path"]),
                symbol=_none_if_empty(item.get("symbol")),
                confidence=str(item.get("confidence") or "medium"),
                reason=str(item.get("reason") or ""),
                profile=profile,
                relationship=str(item.get("relationship") or "edit_surface"),
                item_type=str(item.get("type") or _artifact_item_type(str(item["path"]), None)),
                source_rank=3,
            )
            if candidate is not None:
                candidates.append(candidate)

    if neighborhood:
        for section_name, payload in neighborhood.get("sections", {}).items():
            for item in payload.get("items", []):
                candidate = _change_plan_candidate(
                    path=str(item["path"]),
                    symbol=_none_if_empty(item.get("symbol")),
                    confidence=str(item.get("confidence") or "medium"),
                    reason=str(item.get("explanation") or item.get("reason") or ""),
                    profile=profile,
                    relationship=str(item.get("relationship") or section_name),
                    item_type=str(item.get("type") or _artifact_item_type(str(item["path"]), None)),
                    source_rank=2,
                )
                if candidate is not None:
                    candidates.append(candidate)

    if semantic:
        for item in semantic.get("primary_semantic_context", []):
            candidate = _change_plan_candidate(
                path=str(item["path"]),
                symbol=_none_if_empty(item.get("symbol")),
                confidence=str(item.get("confidence") or "medium"),
                reason=str(item.get("explanation") or ""),
                profile=profile,
                relationship="semantic_primary",
                item_type=_artifact_item_type(str(item["path"]), None),
                source_rank=4,
            )
            if candidate is not None:
                candidates.append(candidate)
        for item in semantic.get("secondary_semantic_context", []):
            candidate = _change_plan_candidate(
                path=str(item["path"]),
                symbol=_none_if_empty(item.get("symbol")),
                confidence=str(item.get("confidence") or "medium"),
                reason=str(item.get("explanation") or ""),
                profile=profile,
                relationship="semantic_secondary",
                item_type=_artifact_item_type(str(item["path"]), None),
                source_rank=5,
            )
            if candidate is not None:
                candidates.append(candidate)

    merged = _merge_change_plan_candidates(candidates)
    required_edits = _ordered_change_candidates(merged, bucket="required", limit=min(limit, 5))
    likely_edits = _ordered_change_candidates(merged, bucket="likely", limit=min(limit, 6))
    optional_edits = _ordered_change_candidates(merged, bucket="optional", limit=min(limit, 6))
    validation_impact = _derive_validation_impact(profile, required_edits, likely_edits, optional_edits, limit=min(limit, 6))
    recommended_sequence = _derive_recommended_sequence(
        profile,
        required_edits,
        likely_edits,
        validation_impact,
        limit=min(6, limit),
    )

    return {
        "query": query,
        "query_kind": query_kind,
        "anchor": anchor,
        "matched_definitions": matched_definitions,
        "total_matches": total_matches,
        "required_edits": required_edits,
        "likely_edits": likely_edits,
        "optional_edits": optional_edits,
        "validation_impact": validation_impact,
        "recommended_sequence": recommended_sequence,
        "notes": [
            "This plan is bounded and confidence-aware. It prioritizes direct structural companions over distant semantic examples.",
        ],
    }


def _semantic_context_for_symbol(
    connection: sqlite3.Connection,
    symbol_query: str,
    limit: int,
) -> dict[str, object]:
    """Return semantic context anchored on one resolved symbol."""

    definition_data = find_definition(connection, symbol_query, limit=max(1, min(limit, 4)), include_usages=False)
    matches = definition_data["matches"]
    if not matches:
        return {
            "query": symbol_query,
            "query_kind": "symbol",
            "anchor": None,
            "matched_definitions": [],
            "total_matches": 0,
            "primary_semantic_context": [],
            "secondary_semantic_context": [],
        }

    anchor_match = matches[0]
    anchor_chunk = _semantic_chunk_from_definition_match(connection, anchor_match)
    neighborhood = dependency_neighborhood(connection, symbol_query=symbol_query, limit=min(6, limit))
    primary_items = _semantic_primary_items_from_sections(
        connection,
        neighborhood["sections"],
        anchor_chunk=anchor_chunk,
        anchor_query=symbol_query,
        limit=limit,
    )
    secondary_items = _semantic_secondary_items_for_anchor(
        connection,
        anchor_chunk=anchor_chunk,
        anchor_query=symbol_query,
        structural_items=primary_items,
        limit=limit,
    )
    return {
        "query": symbol_query,
        "query_kind": "symbol",
        "anchor": _semantic_anchor_summary(anchor_chunk),
        "matched_definitions": [_compact_match_summary(item) for item in matches],
        "total_matches": definition_data["total_matches"],
        "primary_semantic_context": primary_items[:limit],
        "secondary_semantic_context": secondary_items[:limit],
    }


def _semantic_context_for_file(
    connection: sqlite3.Connection,
    file_path: str,
    limit: int,
) -> dict[str, object]:
    """Return semantic context anchored on one indexed file."""

    context = file_context(connection, file_path)
    anchor_chunk = _semantic_chunk_from_file_context(connection, context)
    neighborhood = dependency_neighborhood(connection, file_path=file_path, limit=min(6, limit))
    primary_items = _semantic_primary_items_from_sections(
        connection,
        neighborhood["sections"],
        anchor_chunk=anchor_chunk,
        anchor_query=context["moodle_path"],
        limit=limit,
    )
    secondary_items = _semantic_secondary_items_for_anchor(
        connection,
        anchor_chunk=anchor_chunk,
        anchor_query=context["moodle_path"],
        structural_items=primary_items,
        limit=limit,
    )
    return {
        "query": context["moodle_path"],
        "query_kind": "file",
        "anchor": _semantic_anchor_summary(anchor_chunk),
        "matched_definitions": [],
        "total_matches": 1,
        "primary_semantic_context": primary_items[:limit],
        "secondary_semantic_context": secondary_items[:limit],
    }


def _semantic_context_for_query(
    connection: sqlite3.Connection,
    query_text: str,
    limit: int,
) -> dict[str, object]:
    """Return bounded semantic context for a free-text query."""

    query_tokens = _semantic_focus_tokens(query_text)
    query_intent = _semantic_query_intent(query_tokens)
    candidates = _collect_matching_semantic_chunks(connection, query_tokens, language=None, limit=80)
    ranked = _semantic_rank_query_candidates(
        connection,
        query_text,
        query_tokens,
        candidates,
        limit=limit * 2,
    )
    primary = _semantic_query_primary_items(ranked, query_intent, limit=min(limit, 5))
    secondary = _semantic_query_secondary_items(
        ranked,
        primary,
        query_intent,
        limit=min(len(ranked), limit * 2),
    )
    return {
        "query": query_text,
        "query_kind": "query",
        "anchor": None,
        "matched_definitions": [],
        "total_matches": len(ranked),
        "primary_semantic_context": _semantic_public_query_items(primary),
        "secondary_semantic_context": _semantic_public_query_items(secondary),
    }


def _semantic_query_primary_items(
    ranked: list[dict[str, object]],
    query_intent: dict[str, bool],
    *,
    limit: int,
) -> list[dict[str, object]]:
    """Return the highest-signal free-text primary set.

    When a query explicitly asks for external API examples plus PHPUnit
    coverage, agents benefit from seeing both sides of the pattern quickly:
    an implementation and a concrete test. This keeps the primary set
    representative without changing the endpoint shape or broadening coverage.
    """

    if not (query_intent["external_api"] and query_intent["tests"]):
        return ranked[:limit]

    primary: list[dict[str, object]] = []
    seen_paths: set[str] = set()
    seen_pairs: set[str] = set()

    pair_groups = _semantic_query_pair_groups(ranked)
    for pair_id, items in pair_groups:
        if len(primary) >= limit:
            break
        seen_pairs.add(pair_id)
        for item in items:
            path = str(item["path"])
            if path in seen_paths:
                continue
            seen_paths.add(path)
            primary.append(item)
            if len(primary) >= limit:
                break

    for item in ranked:
        path = str(item["path"])
        if path in seen_paths:
            continue
        pair_id = str(item.get("_pair_id") or "")
        if pair_id and pair_id in seen_pairs:
            continue
        seen_paths.add(path)
        primary.append(item)
        if len(primary) >= limit:
            break
    return primary[:limit]


def _semantic_query_secondary_items(
    ranked: list[dict[str, object]],
    primary: list[dict[str, object]],
    query_intent: dict[str, bool],
    *,
    limit: int,
) -> list[dict[str, object]]:
    """Return free-text secondary items with primary overlap removed."""

    primary_paths = {str(item["path"]) for item in primary}
    primary_pairs = {str(item.get("_pair_id") or "") for item in primary if item.get("_pair_id")}

    secondary: list[dict[str, object]] = []
    for item in ranked:
        path = str(item["path"])
        pair_id = str(item.get("_pair_id") or "")
        if path in primary_paths:
            continue
        if pair_id and pair_id in primary_pairs:
            continue
        secondary.append(item)
        if len(secondary) >= limit:
            break

    if query_intent["external_api"] and query_intent["tests"]:
        return secondary[: min(limit, 10)]
    return secondary[:limit]


def _semantic_primary_items_from_sections(
    connection: sqlite3.Connection,
    sections: dict[str, dict[str, object]],
    *,
    anchor_chunk: SemanticChunk,
    anchor_query: str,
    limit: int,
) -> list[dict[str, object]]:
    """Return bounded anchor-local semantic items from dependency sections."""

    items = [
        _semantic_anchor_item(anchor_chunk),
    ]
    for section_name, payload in sections.items():
        section_candidates: list[dict[str, object]] = []
        for raw_item in list(payload.get("items", []))[:6]:
            chunk = _semantic_chunk_from_reference(
                connection,
                path=str(raw_item["path"]),
                symbol=str(raw_item.get("symbol") or "") or None,
            )
            if chunk is None:
                continue
            lexical = _semantic_overlap_score(anchor_query, chunk.text)
            semantic = _semantic_similarity(anchor_chunk.text, chunk.text)
            raw_score = (
                _semantic_structural_base(section_name, str(raw_item.get("confidence") or "medium"))
                + _semantic_anchor_section_bonus(anchor_chunk, section_name, raw_item)
                + lexical * 0.08
                + semantic * 0.1
                + (0.03 if _same_component_root(anchor_chunk.path, chunk.path) else 0.0)
            )
            score = _semantic_soft_score(raw_score, cap=0.94)
            retrieval_sources = ["structural"]
            if lexical >= 0.12:
                retrieval_sources.append("lexical")
            if semantic >= 0.18:
                retrieval_sources.append("semantic")
            retrieval_sources.append("rerank")
            section_candidates.append(
                {
                    "path": chunk.path,
                    "symbol": chunk.symbol,
                    "chunk_id": chunk.chunk_id,
                    "score": round(score, 3),
                    "retrieval_sources": retrieval_sources,
                    "explanation": str(raw_item.get("explanation") or ""),
                    "why_relevant_to_anchor": _semantic_structural_relevance(section_name, raw_item, anchor_chunk),
                    "snippet": chunk.snippet or chunk.summary,
                    "summary": chunk.summary,
                    "confidence": raw_item.get("confidence", "medium"),
                    "result_kind": "local_context",
                    "_section_priority": _semantic_section_item_priority(
                        anchor_chunk,
                        section_name,
                        raw_item,
                        chunk,
                    ),
                }
            )
        section_candidates.sort(
            key=lambda item: (
                int(item.get("_section_priority", 99)),
                -float(item["score"]),
                str(item["path"]),
                str(item.get("symbol") or ""),
            )
        )
        for item in section_candidates:
            item.pop("_section_priority", None)
        items.extend(
            section_candidates[: _semantic_primary_section_limit(anchor_chunk, section_name)]
        )
    return _merge_semantic_results(items)[: min(limit, 8)]


def _semantic_secondary_items_for_anchor(
    connection: sqlite3.Connection,
    *,
    anchor_chunk: SemanticChunk,
    anchor_query: str,
    structural_items: list[dict[str, object]],
    limit: int,
) -> list[dict[str, object]]:
    """Return semantically similar examples constrained by the anchor."""

    excluded = {str(item["chunk_id"]) for item in structural_items}
    query_tokens = _semantic_focus_tokens(anchor_query + " " + anchor_chunk.text)
    component_only = anchor_chunk.file_role in {"locallib_file", "lib_file"} and anchor_chunk.symbol_type == "method"
    candidates = _collect_component_semantic_chunks(
        connection,
        component_name=anchor_chunk.component,
        language=anchor_chunk.language,
        limit=80,
    )
    candidates.extend(
        _collect_matching_semantic_chunks(
            connection,
            query_tokens,
            language=anchor_chunk.language,
            limit=80,
        )
    )
    ranked: list[dict[str, object]] = []
    for chunk in _deduplicate_semantic_chunks(candidates):
        if chunk.chunk_id in excluded or chunk.chunk_id == anchor_chunk.chunk_id:
            continue
        if chunk.path == anchor_chunk.path:
            continue
        if component_only and chunk.component != anchor_chunk.component:
            continue
        if anchor_chunk.file_role == "services_definition" and chunk.source_kind == "service":
            continue
        lexical = _semantic_overlap_score(anchor_query + " " + anchor_chunk.title, chunk.text)
        semantic = _semantic_similarity(anchor_chunk.text, chunk.text)
        if lexical <= 0.0 and semantic < 0.24:
            continue
        role_bonus = 0.12 if chunk.file_role == anchor_chunk.file_role else 0.0
        language_bonus = 0.1 if chunk.language == anchor_chunk.language else 0.0
        symbol_bonus = 0.08 if chunk.symbol_type and chunk.symbol_type == anchor_chunk.symbol_type else 0.0
        component_bonus = 0.08 if chunk.component == anchor_chunk.component else 0.0
        score = _semantic_soft_score(
            lexical * 0.18 + semantic * 0.28 + role_bonus + language_bonus + symbol_bonus + component_bonus,
            cap=0.84,
        )
        if score < (0.42 if component_only else 0.36):
            continue
        ranked.append(
            {
                "path": chunk.path,
                "symbol": chunk.symbol,
                "chunk_id": chunk.chunk_id,
                "score": round(score, 3),
                "retrieval_sources": _semantic_sources(lexical, semantic),
                "explanation": _semantic_similar_explanation(anchor_chunk, chunk),
                "why_relevant_to_anchor": _semantic_similar_relevance(anchor_chunk, chunk, lexical, semantic),
                "snippet": chunk.snippet or chunk.summary,
                "summary": chunk.summary,
                "confidence": "medium" if score < 0.62 else "high",
                "result_kind": "similar_example",
            }
        )
    merged = _merge_semantic_results(ranked)
    return merged[: min(limit, 4 if component_only else 6)]


def _semantic_rank_query_candidates(
    connection: sqlite3.Connection,
    query_text: str,
    query_tokens: list[str],
    candidates: list[SemanticChunk],
    *,
    limit: int,
) -> list[dict[str, object]]:
    """Return bounded free-text semantic matches."""

    query_intent = _semantic_query_intent(query_tokens)
    prepared: list[tuple[SemanticChunk, float, float, dict[str, object]]] = []
    candidate_paths = {chunk.path for chunk in _deduplicate_semantic_chunks(candidates)}
    for chunk in _deduplicate_semantic_chunks(candidates):
        lexical = _semantic_overlap_score(query_text, chunk.text)
        semantic = _semantic_similarity(query_text, chunk.text)
        if lexical <= 0.0 and semantic < 0.2:
            continue
        raw_score = (
            lexical * 0.35
            + semantic * 0.32
            + (0.1 if any(token in chunk.text.lower() for token in query_tokens[:2]) else 0.0)
            + _semantic_query_kind_bonus(query_tokens, chunk)
        )
        score = _semantic_soft_score(raw_score, cap=0.9)
        score *= _semantic_query_intent_multiplier(query_intent, chunk)
        pair_meta = _semantic_query_pair_metadata(connection, chunk, candidate_paths)
        score += _semantic_query_pair_bonus(query_intent, chunk, pair_meta)
        score = round(min(0.9, score), 3)
        minimum_score = 0.33 if pair_meta["has_pair"] else 0.35
        if score < minimum_score:
            continue
        prepared.append(
            (
                chunk,
                lexical,
                semantic,
                {
                    "score": score,
                    "pair_id": pair_meta["pair_id"],
                    "paired_path": pair_meta["paired_path"],
                    "_pair_id": pair_meta["pair_id"],
                    "_paired_path": pair_meta["paired_path"],
                    "has_pair": pair_meta["has_pair"],
                },
            )
        )
    ranked: list[dict[str, object]] = []
    for chunk, lexical, semantic, pair_meta in prepared:
        explanation = _semantic_query_explanation(query_text, chunk, pair_meta)
        if not explanation:
            continue
        ranked.append(
            {
                "path": chunk.path,
                "symbol": chunk.symbol,
                "chunk_id": chunk.chunk_id,
                "score": pair_meta["score"],
                "retrieval_sources": _semantic_sources(lexical, semantic),
                "explanation": explanation,
                "why_relevant_to_anchor": _semantic_query_relevance(query_tokens, chunk, pair_meta),
                "snippet": chunk.snippet or chunk.summary,
                "summary": chunk.summary,
                "confidence": "medium" if pair_meta["score"] < 0.62 else "high",
                "result_kind": "query_match",
                "_pair_id": pair_meta["_pair_id"],
                "_paired_path": pair_meta["_paired_path"],
            }
        )
    ranked.sort(key=lambda item: (-float(item["score"]), str(item["path"]), str(item.get("symbol") or "")))
    return _merge_semantic_results(ranked)[:limit]


def _semantic_public_query_items(items: list[dict[str, object]]) -> list[dict[str, object]]:
    """Strip free-text pairing internals before returning results."""

    public_items: list[dict[str, object]] = []
    for item in items:
        public_items.append(
            {
                key: value
                for key, value in item.items()
                if key not in {"pair_id", "paired_path", "_pair_id", "_paired_path"}
            }
        )
    return public_items


def _semantic_anchor_item(anchor_chunk: SemanticChunk) -> dict[str, object]:
    """Return the resolved anchor as the first semantic-context item."""

    return {
        "path": anchor_chunk.path,
        "symbol": anchor_chunk.symbol,
        "chunk_id": anchor_chunk.chunk_id,
        "score": 1.0,
        "retrieval_sources": ["structural", "rerank"],
        "explanation": "Resolved anchor definition/file for this query; start here before broadening into linked context or similar examples.",
        "why_relevant_to_anchor": "This is the structural anchor the semantic retrieval is centered on.",
        "snippet": anchor_chunk.snippet or anchor_chunk.summary,
        "summary": anchor_chunk.summary,
        "confidence": "high",
        "result_kind": "anchor",
    }


def _semantic_primary_section_limit(anchor_chunk: SemanticChunk, section_name: str) -> int:
    """Return a small section cap tuned to the current anchor shape.

    The semantic layer should feel like “inspect these next”, not a flattened
    dump of the whole dependency neighborhood. These caps stay intentionally
    conservative for broad locallib methods and service-definition files.
    """

    if anchor_chunk.file_role == "services_definition":
        return {
            "likely_callers": 1,
            "likely_callees": 3,
            "linked_services": 2,
            "linked_tests": 2,
            "linked_framework": 1,
        }.get(section_name, 2)
    if anchor_chunk.file_role == "external_api_class":
        return {
            "likely_callers": 2,
            "linked_tests": 2,
            "linked_services": 2,
            "linked_rendering_artifacts": 0,
            "linked_framework": 0,
        }.get(section_name, 2)
    if anchor_chunk.file_role in {"locallib_file", "lib_file"} and anchor_chunk.symbol_type == "method":
        return {
            "likely_callers": 2,
            "linked_rendering_artifacts": 2,
            "linked_tests": 1,
            "linked_framework": 1,
        }.get(section_name, 2)
    if "provider" in anchor_chunk.path:
        return {
            "linked_forms": 3,
            "linked_framework": 2,
            "likely_callees": 2,
        }.get(section_name, 2)
    if anchor_chunk.language == "js":
        return {
            "likely_callers": 2,
            "likely_callees": 3,
            "linked_javascript": 3,
            "linked_build_artifacts": 1,
        }.get(section_name, 2)
    return 3


def _semantic_section_item_priority(
    anchor_chunk: SemanticChunk,
    section_name: str,
    raw_item: dict[str, object],
    chunk: SemanticChunk,
) -> int:
    """Return a small priority bias inside one semantic primary section."""

    item_type = str(raw_item.get("type") or "")
    relationship = str(raw_item.get("relationship") or "")

    if section_name == "linked_rendering_artifacts":
        if item_type == "output_class":
            return 0
        if item_type == "renderer_file":
            return 1
        if item_type == "template_file":
            return 2
        return 5
    if section_name == "linked_forms":
        if item_type == "form_class" and str(raw_item.get("chain_role") or "direct") == "direct":
            return 0
        if item_type == "form_class":
            return 1
        if item_type == "framework_base":
            return 2
        return 5
    if section_name == "linked_tests":
        if "/tests/external/" in chunk.path:
            return 0
        return 2
    if section_name == "likely_callers":
        if relationship == "service_definition":
            return 0
        if relationship in {"instance_method_call", "static_method_call", "function_call"}:
            return 1
        return 5
    if anchor_chunk.file_role == "services_definition" and section_name in {"linked_services", "likely_callees"}:
        if chunk.file_role == "external_api_class":
            return 0
        if chunk.path.endswith("/externallib.php"):
            return 1
        return 4
    return 3


def _semantic_anchor_summary(anchor_chunk: SemanticChunk) -> dict[str, object]:
    """Return compact anchor metadata for semantic-context responses."""

    return {
        "path": anchor_chunk.path,
        "symbol": anchor_chunk.symbol,
        "chunk_id": anchor_chunk.chunk_id,
        "component": anchor_chunk.component,
        "file_role": anchor_chunk.file_role,
        "language": anchor_chunk.language,
        "symbol_type": anchor_chunk.symbol_type,
        "line": anchor_chunk.line,
    }


def _semantic_structural_base(section_name: str, confidence: str) -> float:
    """Return the structural score floor for one dependency-neighborhood section."""

    section_weights = {
        "likely_callers": 0.5,
        "likely_callees": 0.48,
        "linked_tests": 0.49,
        "linked_services": 0.47,
        "linked_rendering_artifacts": 0.43,
        "linked_forms": 0.44,
        "linked_framework": 0.36,
        "linked_javascript": 0.45,
        "linked_build_artifacts": 0.39,
    }
    confidence_bonus = {"high": 0.08, "medium": 0.04, "low": 0.0}.get(confidence, 0.0)
    return section_weights.get(section_name, 0.56) + confidence_bonus


def _semantic_anchor_section_bonus(
    anchor_chunk: SemanticChunk,
    section_name: str,
    item: dict[str, object],
) -> float:
    """Return a small anchor-aware bias for primary semantic context ordering."""

    bonus = 0.0
    if anchor_chunk.file_role in {"locallib_file", "lib_file"}:
        if section_name == "linked_rendering_artifacts":
            bonus += 0.12
        if section_name == "linked_framework":
            bonus -= 0.06
    if anchor_chunk.file_role == "external_api_class" or anchor_chunk.file_role == "services_definition":
        if section_name in {"linked_services", "linked_tests"}:
            bonus += 0.08
        if section_name in {"linked_rendering_artifacts", "linked_framework"}:
            bonus -= 0.08
    if "provider" in anchor_chunk.path:
        if section_name == "linked_forms":
            bonus += 0.12
        if section_name == "linked_framework":
            bonus += 0.04
    if anchor_chunk.language == "js":
        if section_name == "linked_javascript":
            bonus += 0.08
        if section_name == "linked_build_artifacts":
            bonus += 0.06

    if str(item.get("type")) == "template_file" and section_name == "linked_rendering_artifacts":
        bonus += 0.04
    return bonus


def _semantic_structural_relevance(
    section_name: str,
    item: dict[str, object],
    anchor_chunk: SemanticChunk,
) -> str:
    """Explain why one structural companion matters to the current anchor."""

    relationship = str(item.get("relationship") or "")
    if section_name == "likely_callers":
        return f"Appears in the anchor's bounded dependency neighborhood as a likely caller ({relationship}); inspect it when tracing how {anchor_chunk.symbol or anchor_chunk.path} is entered."
    if section_name == "likely_callees":
        return f"Appears in the anchor's bounded dependency neighborhood as a likely callee ({relationship}); inspect it when tracing what {anchor_chunk.symbol or anchor_chunk.path} directly depends on."
    if section_name == "linked_tests":
        return f"Provides concrete automated coverage for {anchor_chunk.symbol or anchor_chunk.path}; inspect it when changing behavior or API expectations."
    if section_name == "linked_rendering_artifacts":
        return f"Belongs to the anchor's rendering flow; inspect it when changing rendered data, renderer logic, or templates around {anchor_chunk.symbol or anchor_chunk.path}."
    if section_name == "linked_forms":
        return f"Belongs to the anchor's form workflow; inspect it when changing fields, defaults, or validation around {anchor_chunk.symbol or anchor_chunk.path}."
    if section_name == "linked_javascript":
        return f"Belongs to the anchor's JavaScript module flow; inspect it when changing imported APIs or inherited client-side behavior."
    return f"Direct structural companion for {anchor_chunk.symbol or anchor_chunk.path}; inspect it if the surrounding feature slice changes."


def _semantic_similar_explanation(anchor_chunk: SemanticChunk, chunk: SemanticChunk) -> str:
    """Return a concise explanation for one semantically similar example."""

    kind = _semantic_human_kind(chunk)
    if chunk.file_role == anchor_chunk.file_role:
        return f"Semantically similar {kind} with the same Moodle role ({chunk.file_role}); inspect it if you need a comparable implementation pattern, test shape, or review point for this anchor."
    return f"Semantically similar {kind} retrieved from the same codebase; inspect it if you need a comparable implementation pattern or fallback example for this anchor."


def _semantic_similar_relevance(
    anchor_chunk: SemanticChunk,
    chunk: SemanticChunk,
    lexical: float,
    semantic: float,
) -> str:
    """Explain the concrete overlap between the anchor and a similar example."""

    reasons: list[str] = []
    if chunk.file_role == anchor_chunk.file_role:
        reasons.append(f"shares the same file role ({chunk.file_role})")
    if chunk.language == anchor_chunk.language:
        reasons.append(f"uses the same language ({chunk.language})")
    if chunk.symbol_type and chunk.symbol_type == anchor_chunk.symbol_type:
        reasons.append(f"matches the same symbol kind ({chunk.symbol_type})")
    if chunk.component == anchor_chunk.component:
        reasons.append("stays within the same component")
    if lexical >= 0.2:
        reasons.append("shares meaningful vocabulary with the anchor")
    if semantic >= 0.35:
        reasons.append("has a close hashed-vector similarity to the anchor chunk")
    if not reasons:
        reasons.append("is the closest bounded similar example found for this anchor")
    return f"Retrieved because it {', '.join(reasons)}."


def _semantic_query_explanation(
    query_text: str,
    chunk: SemanticChunk,
    pair_meta: dict[str, object],
) -> str:
    """Return a decision-ready explanation for one free-text retrieval result."""

    kind = _semantic_human_kind(chunk)
    paired_path = str(pair_meta.get("paired_path") or "")
    if chunk.file_role == "services_definition":
        return f"Service registration matched the free-text query '{query_text}'; inspect it when you need a concrete web-service entrypoint example or service wiring reference."
    if chunk.file_role == "external_api_class":
        if paired_path:
            return (
                f"External API implementation matched the free-text query '{query_text}' and has a concrete paired PHPUnit file "
                f"({paired_path}); inspect it when you need a canonical implementation example plus coverage."
            )
        return f"External API implementation matched the free-text query '{query_text}'; inspect it when you need a concrete web-service method example or API contract pattern."
    if chunk.source_kind == "test":
        if paired_path:
            return (
                f"Concrete test matched the free-text query '{query_text}' and pairs with implementation "
                f"{paired_path}; inspect it when you need PHPUnit-backed examples for the same API pattern."
            )
        return f"Concrete test matched the free-text query '{query_text}'; inspect it when you need PHPUnit-backed examples or expected behavior for a similar workflow."
    return f"{kind.capitalize()} matched the free-text query '{query_text}'; inspect it if you need a concrete code example or local implementation context."


def _semantic_query_relevance(
    query_tokens: list[str],
    chunk: SemanticChunk,
    pair_meta: dict[str, object],
) -> str:
    """Return a concise relevance explanation for free-text retrieval."""

    shared = [token for token in query_tokens if token in chunk.text.lower()][:4]
    paired_path = str(pair_meta.get("paired_path") or "")
    if paired_path:
        pair_note = f" Paired with {paired_path}."
    else:
        pair_note = ""
    if shared:
        return f"Matched the query through shared terms: {', '.join(shared)}.{pair_note}"
    return f"Matched the query through bounded lexical and hashed-vector similarity.{pair_note}"


def _semantic_sources(lexical: float, semantic: float) -> list[str]:
    """Return retrieval-source labels for one semantic result."""

    sources: list[str] = []
    if lexical > 0.0:
        sources.append("lexical")
    if semantic >= 0.18:
        sources.append("semantic")
    sources.append("rerank")
    return sources


def _semantic_query_pair_groups(
    ranked: list[dict[str, object]],
) -> list[tuple[str, list[dict[str, object]]]]:
    """Return strong implementation/test groups for free-text ranking."""

    grouped: dict[str, list[dict[str, object]]] = {}
    for item in ranked:
        pair_id = str(item.get("_pair_id") or "")
        if not pair_id:
            continue
        grouped.setdefault(pair_id, []).append(item)

    complete_groups: list[tuple[str, list[dict[str, object]]]] = []
    for pair_id, items in grouped.items():
        has_test = any(_is_concrete_test_path(str(item["path"])) for item in items)
        has_impl = any(_is_service_implementation_path(str(item["path"])) for item in items)
        if not (has_test and has_impl):
            continue
        ordered = sorted(
            items,
            key=lambda item: (
                0 if _is_service_implementation_path(str(item["path"])) else 1,
                -float(item["score"]),
                str(item["path"]),
            ),
        )
        complete_groups.append((pair_id, ordered[:2]))

    complete_groups.sort(
        key=lambda group: (
            -max(float(item["score"]) for item in group[1]),
            group[0],
        )
    )
    return complete_groups


def _semantic_query_pair_metadata(
    connection: sqlite3.Connection,
    chunk: SemanticChunk,
    candidate_paths: set[str],
) -> dict[str, object]:
    """Return implementation/test pairing metadata for free-text results."""

    path = chunk.path
    pair_id = ""
    paired_path = ""

    if _is_service_implementation_path(path):
        pair_id = path
        for candidate in _semantic_query_test_candidates_for_path(connection, path):
            if candidate in candidate_paths:
                paired_path = candidate
                break
    elif _is_concrete_test_path(path):
        implementation_path = _semantic_query_implementation_for_test_path(path)
        if implementation_path:
            pair_id = implementation_path
            if implementation_path in candidate_paths:
                paired_path = implementation_path
    return {
        "pair_id": pair_id,
        "paired_path": paired_path,
        "has_pair": bool(pair_id and paired_path),
    }


def _semantic_query_pair_bonus(
    query_intent: dict[str, bool],
    chunk: SemanticChunk,
    pair_meta: dict[str, object],
) -> float:
    """Return a small bonus or penalty for paired free-text examples."""

    if not (query_intent["external_api"] and query_intent["tests"]):
        return 0.0
    path = chunk.path
    has_pair = bool(pair_meta["has_pair"])
    if _is_service_implementation_path(path):
        return 0.12 if has_pair else -0.02
    if _is_concrete_test_path(path):
        return 0.08 if has_pair else -0.08
    return 0.0


def _semantic_query_test_candidates_for_path(
    connection: sqlite3.Connection,
    implementation_path: str,
) -> list[str]:
    """Return likely concrete tests for one implementation path."""

    webservice_stub = {"resolved_target_file": implementation_path, "classname": None}
    return [item["file"] for item in _service_tests_for_definition(connection, webservice_stub)]


def _semantic_query_implementation_for_test_path(path: str) -> str | None:
    """Return the most likely implementation paired with one concrete test path."""

    if path.endswith("/tests/externallib_test.php") or path.endswith("/tests/externallib_advanced_testcase.php"):
        return path.replace("/tests/externallib_test.php", "/externallib.php").replace(
            "/tests/externallib_advanced_testcase.php",
            "/externallib.php",
        )
    if "/tests/external/" in path and path.endswith("_test.php"):
        component_root, class_suffix = path.split("/tests/external/", 1)
        class_name = class_suffix.removesuffix("_test.php")
        return f"{component_root}/classes/external/{class_name}.php"
    return None


def _semantic_soft_score(raw_score: float, *, cap: float) -> float:
    """Return a bounded semantic score with softer saturation.

    This keeps anchor-local items strong without collapsing too many companions
    onto the same 1.0 ceiling.
    """

    if raw_score <= 0.0:
        return 0.0
    scaled = raw_score / (1.0 + 0.35 * max(raw_score - 0.45, 0.0))
    return min(cap, scaled)


def _merge_semantic_results(items: list[dict[str, object]]) -> list[dict[str, object]]:
    """Deduplicate semantic results by chunk id while preserving stronger signals."""

    merged: dict[str, dict[str, object]] = {}
    for item in items:
        key = str(item["path"])
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(item)
            continue
        if float(item["score"]) > float(existing["score"]):
            existing.update(item)
        sources = sorted(set(existing.get("retrieval_sources", [])) | set(item.get("retrieval_sources", [])))
        existing["retrieval_sources"] = sources
    return sorted(
        merged.values(),
        key=lambda item: (
            _semantic_result_priority(item),
            -float(item["score"]),
            str(item["path"]),
            str(item.get("symbol") or ""),
        ),
    )


def _semantic_result_priority(item: dict[str, object]) -> int:
    """Return a stable ordering priority for semantic results."""

    result_kind = str(item.get("result_kind") or "")
    priorities = {
        "anchor": 0,
        "local_context": 1,
        "query_match": 1,
        "similar_example": 2,
    }
    return priorities.get(result_kind, 5)


def _deduplicate_semantic_chunks(chunks: list[SemanticChunk]) -> list[SemanticChunk]:
    """Return semantic chunks keyed by deterministic chunk id."""

    merged: dict[str, SemanticChunk] = {}
    for chunk in chunks:
        merged.setdefault(chunk.chunk_id, chunk)
    return list(merged.values())


def _collect_component_semantic_chunks(
    connection: sqlite3.Connection,
    *,
    component_name: str,
    language: str | None,
    limit: int,
) -> list[SemanticChunk]:
    """Collect a bounded structural-chunk pool inside one component."""

    chunks: list[SemanticChunk] = []
    chunks.extend(_semantic_symbol_chunks_for_component(connection, component_name, language=language, limit=limit))
    chunks.extend(_semantic_js_chunks_for_component(connection, component_name, limit=max(10, limit // 2)))
    chunks.extend(_semantic_test_chunks_for_component(connection, component_name, language=language, limit=max(10, limit // 3)))
    chunks.extend(_semantic_service_chunks_for_component(connection, component_name, limit=max(10, limit // 4)))
    chunks.extend(_semantic_file_chunks_for_component(connection, component_name, language=language, limit=max(10, limit // 3)))
    return chunks


def _collect_matching_semantic_chunks(
    connection: sqlite3.Connection,
    query_tokens: list[str],
    *,
    language: str | None,
    limit: int,
) -> list[SemanticChunk]:
    """Collect a bounded global chunk pool using lexical token filters first."""

    if not query_tokens:
        return []
    chunks: list[SemanticChunk] = []
    chunks.extend(_semantic_symbol_chunks_for_tokens(connection, query_tokens, language=language, limit=limit))
    chunks.extend(_semantic_js_chunks_for_tokens(connection, query_tokens, limit=max(10, limit // 2)))
    chunks.extend(_semantic_test_chunks_for_tokens(connection, query_tokens, language=language, limit=max(10, limit // 3)))
    chunks.extend(_semantic_service_chunks_for_tokens(connection, query_tokens, limit=max(10, limit // 4)))
    chunks.extend(_semantic_file_chunks_for_tokens(connection, query_tokens, language=language, limit=max(10, limit // 3)))
    return chunks


def _semantic_chunk_from_definition_match(
    connection: sqlite3.Connection,
    match: dict[str, object],
) -> SemanticChunk:
    """Build a semantic chunk from one resolved definition payload."""

    if str(match.get("symbol_type")) == "js_module":
        row = connection.execute(
            """
            SELECT
                jm.module_name,
                jm.export_kind,
                jm.export_name,
                jm.superclass_name,
                jm.superclass_module,
                jm.build_file,
                f.moodle_path,
                f.file_role,
                f.absolute_path,
                c.name AS component_name
            FROM js_modules jm
            JOIN files f ON f.id = jm.file_id
            JOIN components c ON c.id = jm.component_id
            WHERE jm.module_name = ?
            LIMIT 1
            """,
            (match.get("module_name") or match.get("fqname"),),
        ).fetchone()
        if row is None:
            raise ValidationError(f"JavaScript module not found in index: {match.get('module_name') or match.get('fqname')}")
        return _semantic_chunk_from_js_row(row)

    row = connection.execute(
        """
        SELECT
            s.name,
            s.fqname,
            s.symbol_type,
            s.signature,
            s.docblock_summary,
            s.return_type,
            s.line,
            f.moodle_path,
            f.file_role,
            f.absolute_path,
            c.name AS component_name
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        JOIN components c ON c.id = s.component_id
        WHERE s.fqname = ?
        LIMIT 1
        """,
        (match["fqname"],),
    ).fetchone()
    if row is None:
        raise ValidationError(f"Definition not found in index: {match['fqname']}")
    return _semantic_chunk_from_symbol_row(row)


def _semantic_chunk_from_file_context(
    connection: sqlite3.Connection,
    context: dict[str, object],
) -> SemanticChunk:
    """Build a semantic chunk from one resolved file-context payload."""

    row = connection.execute(
        """
        SELECT
            f.id,
            f.moodle_path,
            f.file_role,
            f.absolute_path,
            c.name AS component_name
        FROM files f
        JOIN components c ON c.id = f.component_id
        WHERE f.moodle_path = ?
        LIMIT 1
        """,
        (context["moodle_path"],),
    ).fetchone()
    if row is None:
        raise ValidationError(f"File not found in index: {context['moodle_path']}")
    return _semantic_chunk_from_file_row(connection, row)


def _semantic_chunk_from_reference(
    connection: sqlite3.Connection,
    *,
    path: str,
    symbol: str | None,
) -> SemanticChunk | None:
    """Resolve a neighborhood reference into a semantic chunk."""

    if symbol:
        if "/" in symbol and "\\" not in symbol:
            row = connection.execute(
                """
                SELECT
                    jm.module_name,
                    jm.export_kind,
                    jm.export_name,
                    jm.superclass_name,
                    jm.superclass_module,
                    jm.build_file,
                    f.moodle_path,
                    f.file_role,
                    f.absolute_path,
                    c.name AS component_name
                FROM js_modules jm
                JOIN files f ON f.id = jm.file_id
                JOIN components c ON c.id = jm.component_id
                WHERE jm.module_name = ?
                LIMIT 1
                """,
                (symbol,),
            ).fetchone()
            if row is not None:
                return _semantic_chunk_from_js_row(row)
        else:
            row = connection.execute(
                """
                SELECT
                    s.name,
                    s.fqname,
                    s.symbol_type,
                    s.signature,
                    s.docblock_summary,
                    s.return_type,
                    s.line,
                    f.moodle_path,
                    f.file_role,
                    f.absolute_path,
                    c.name AS component_name
                FROM symbols s
                JOIN files f ON f.id = s.file_id
                JOIN components c ON c.id = s.component_id
                WHERE s.fqname = ?
                LIMIT 1
                """,
                (symbol,),
            ).fetchone()
            if row is not None:
                return _semantic_chunk_from_symbol_row(row)

    repository = _get_indexed_repository_metadata(connection)
    try:
        file_row = _resolve_file_row(connection, repository, path)
    except ValidationError:
        return None
    return _semantic_chunk_from_file_row(connection, file_row)


def _semantic_symbol_chunks_for_component(
    connection: sqlite3.Connection,
    component_name: str,
    *,
    language: str | None,
    limit: int,
) -> list[SemanticChunk]:
    """Return PHP symbol chunks within one component."""

    params: list[object] = [component_name]
    language_clause = ""
    if language == "php":
        language_clause = "AND f.extension = '.php'"
    rows = connection.execute(
        f"""
        SELECT
            s.name,
            s.fqname,
            s.symbol_type,
            s.signature,
            s.docblock_summary,
            s.return_type,
            s.line,
            f.moodle_path,
            f.file_role,
            f.absolute_path,
            c.name AS component_name
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        JOIN components c ON c.id = s.component_id
        WHERE c.name = ?
        {language_clause}
        ORDER BY f.moodle_path, s.line
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    return [_semantic_chunk_from_symbol_row(row) for row in rows]


def _semantic_js_chunks_for_component(
    connection: sqlite3.Connection,
    component_name: str,
    *,
    limit: int,
) -> list[SemanticChunk]:
    """Return JS module chunks within one component."""

    rows = connection.execute(
        """
        SELECT
            jm.module_name,
            jm.export_kind,
            jm.export_name,
            jm.superclass_name,
            jm.superclass_module,
            jm.build_file,
            f.moodle_path,
            f.file_role,
            f.absolute_path,
            c.name AS component_name
        FROM js_modules jm
        JOIN files f ON f.id = jm.file_id
        JOIN components c ON c.id = jm.component_id
        WHERE c.name = ?
        ORDER BY f.moodle_path
        LIMIT ?
        """,
        (component_name, limit),
    ).fetchall()
    return [_semantic_chunk_from_js_row(row) for row in rows]


def _semantic_test_chunks_for_component(
    connection: sqlite3.Connection,
    component_name: str,
    *,
    language: str | None,
    limit: int,
) -> list[SemanticChunk]:
    """Return test chunks within one component."""

    if language not in {None, "php"}:
        return []
    rows = connection.execute(
        """
        SELECT
            t.name,
            t.test_type,
            t.related_symbol,
            t.line,
            f.moodle_path,
            f.file_role,
            f.absolute_path,
            c.name AS component_name
        FROM tests t
        JOIN files f ON f.id = t.file_id
        JOIN components c ON c.id = t.component_id
        WHERE c.name = ?
        ORDER BY f.moodle_path, t.line
        LIMIT ?
        """,
        (component_name, limit),
    ).fetchall()
    return [_semantic_chunk_from_test_row(row) for row in rows]


def _semantic_service_chunks_for_component(
    connection: sqlite3.Connection,
    component_name: str,
    *,
    limit: int,
) -> list[SemanticChunk]:
    """Return service-definition chunks within one component."""

    rows = connection.execute(
        """
        SELECT
            ws.service_name,
            ws.classname,
            ws.methodname,
            ws.resolved_target_file,
            ws.line,
            f.moodle_path,
            f.file_role,
            f.absolute_path,
            c.name AS component_name
        FROM webservices ws
        JOIN files f ON f.id = ws.file_id
        JOIN components c ON c.id = ws.component_id
        WHERE c.name = ?
        ORDER BY f.moodle_path, ws.line
        LIMIT ?
        """,
        (component_name, limit),
    ).fetchall()
    return [_semantic_chunk_from_webservice_row(row) for row in rows]


def _semantic_file_chunks_for_component(
    connection: sqlite3.Connection,
    component_name: str,
    *,
    language: str | None,
    limit: int,
) -> list[SemanticChunk]:
    """Return file-level fallback chunks within one component."""

    extension_clause = ""
    params: list[object] = [component_name]
    if language == "js":
        extension_clause = "AND f.extension = '.js'"
    elif language == "php":
        extension_clause = "AND f.extension = '.php'"
    elif language == "template":
        extension_clause = "AND f.extension = '.mustache'"
    params.append(limit)
    rows = connection.execute(
        f"""
        SELECT
            f.id,
            f.moodle_path,
            f.file_role,
            f.absolute_path,
            c.name AS component_name
        FROM files f
        JOIN components c ON c.id = f.component_id
        WHERE c.name = ?
          AND f.file_role IN ('template_file', 'renderer_file', 'output_class', 'services_definition', 'amd_build', 'locallib_file', 'settings_file')
          {extension_clause}
        ORDER BY f.moodle_path
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [_semantic_chunk_from_file_row(connection, row) for row in rows]


def _semantic_symbol_chunks_for_tokens(
    connection: sqlite3.Connection,
    query_tokens: list[str],
    *,
    language: str | None,
    limit: int,
) -> list[SemanticChunk]:
    """Return lexically filtered symbol chunks across the repository."""

    clause, params = _semantic_like_clause(
        ["s.name", "s.fqname", "coalesce(s.docblock_summary, '')", "coalesce(s.signature, '')"],
        query_tokens,
    )
    if not clause:
        return []
    language_clause = ""
    if language == "php":
        language_clause = "AND f.extension = '.php'"
    rows = connection.execute(
        f"""
        SELECT
            s.name,
            s.fqname,
            s.symbol_type,
            s.signature,
            s.docblock_summary,
            s.return_type,
            s.line,
            f.moodle_path,
            f.file_role,
            f.absolute_path,
            c.name AS component_name
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        JOIN components c ON c.id = s.component_id
        WHERE ({clause})
        {language_clause}
        ORDER BY f.moodle_path, s.line
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    return [_semantic_chunk_from_symbol_row(row) for row in rows]


def _semantic_js_chunks_for_tokens(
    connection: sqlite3.Connection,
    query_tokens: list[str],
    *,
    limit: int,
) -> list[SemanticChunk]:
    """Return lexically filtered JS module chunks across the repository."""

    clause, params = _semantic_like_clause(
        [
            "jm.module_name",
            "coalesce(jm.export_name, '')",
            "coalesce(jm.superclass_name, '')",
            "coalesce(jm.superclass_module, '')",
            "f.moodle_path",
        ],
        query_tokens,
    )
    if not clause:
        return []
    rows = connection.execute(
        f"""
        SELECT
            jm.module_name,
            jm.export_kind,
            jm.export_name,
            jm.superclass_name,
            jm.superclass_module,
            jm.build_file,
            f.moodle_path,
            f.file_role,
            f.absolute_path,
            c.name AS component_name
        FROM js_modules jm
        JOIN files f ON f.id = jm.file_id
        JOIN components c ON c.id = jm.component_id
        WHERE {clause}
        ORDER BY f.moodle_path
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    return [_semantic_chunk_from_js_row(row) for row in rows]


def _semantic_test_chunks_for_tokens(
    connection: sqlite3.Connection,
    query_tokens: list[str],
    *,
    language: str | None,
    limit: int,
) -> list[SemanticChunk]:
    """Return lexically filtered test chunks across the repository."""

    if language not in {None, "php"}:
        return []
    clause, params = _semantic_like_clause(
        [
            "t.name",
            "coalesce(t.related_symbol, '')",
            "t.test_type",
            "f.moodle_path",
        ],
        query_tokens,
    )
    if not clause:
        return []
    rows = connection.execute(
        f"""
        SELECT
            t.name,
            t.test_type,
            t.related_symbol,
            t.line,
            f.moodle_path,
            f.file_role,
            f.absolute_path,
            c.name AS component_name
        FROM tests t
        JOIN files f ON f.id = t.file_id
        JOIN components c ON c.id = t.component_id
        WHERE {clause}
        ORDER BY f.moodle_path, t.line
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    return [_semantic_chunk_from_test_row(row) for row in rows]


def _semantic_service_chunks_for_tokens(
    connection: sqlite3.Connection,
    query_tokens: list[str],
    *,
    limit: int,
) -> list[SemanticChunk]:
    """Return lexically filtered service-definition chunks across the repository."""

    clause, params = _semantic_like_clause(
        [
            "ws.service_name",
            "coalesce(ws.classname, '')",
            "coalesce(ws.methodname, '')",
            "coalesce(ws.resolved_target_file, '')",
            "f.moodle_path",
        ],
        query_tokens,
    )
    if not clause:
        return []
    rows = connection.execute(
        f"""
        SELECT
            ws.service_name,
            ws.classname,
            ws.methodname,
            ws.resolved_target_file,
            ws.line,
            f.moodle_path,
            f.file_role,
            f.absolute_path,
            c.name AS component_name
        FROM webservices ws
        JOIN files f ON f.id = ws.file_id
        JOIN components c ON c.id = ws.component_id
        WHERE {clause}
        ORDER BY f.moodle_path, ws.line
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    return [_semantic_chunk_from_webservice_row(row) for row in rows]


def _semantic_file_chunks_for_tokens(
    connection: sqlite3.Connection,
    query_tokens: list[str],
    *,
    language: str | None,
    limit: int,
) -> list[SemanticChunk]:
    """Return lexically filtered file fallback chunks across the repository."""

    clause, params = _semantic_like_clause(
        ["f.moodle_path", "f.file_role", "c.name"],
        query_tokens,
    )
    if not clause:
        return []
    extension_clause = ""
    if language == "js":
        extension_clause = "AND f.extension = '.js'"
    elif language == "php":
        extension_clause = "AND f.extension = '.php'"
    elif language == "template":
        extension_clause = "AND f.extension = '.mustache'"
    rows = connection.execute(
        f"""
        SELECT
            f.id,
            f.moodle_path,
            f.file_role,
            f.absolute_path,
            c.name AS component_name
        FROM files f
        JOIN components c ON c.id = f.component_id
        WHERE ({clause})
          AND f.file_role IN ('template_file', 'renderer_file', 'output_class', 'services_definition', 'amd_build', 'amd_source', 'locallib_file', 'settings_file', 'external_api_class')
          {extension_clause}
        ORDER BY f.moodle_path
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    return [_semantic_chunk_from_file_row(connection, row) for row in rows]


def _semantic_chunk_from_symbol_row(row: sqlite3.Row) -> SemanticChunk:
    """Build one PHP symbol chunk from a symbol row."""

    title = str(row["fqname"])
    summary = " ".join(
        part
        for part in [
            str(row["symbol_type"]),
            str(row["signature"] or ""),
            str(row["docblock_summary"] or ""),
        ]
        if part and part != "None"
    ).strip()
    snippet = _source_snippet(str(row["absolute_path"]), int(row["line"]) if row["line"] is not None else None)
    text = " ".join(
        part
        for part in [
            row["name"],
            row["fqname"],
            row["symbol_type"],
            row["signature"] or "",
            row["docblock_summary"] or "",
            row["return_type"] or "",
            row["moodle_path"],
            row["file_role"],
            row["component_name"],
            _semantic_role_tags(str(row["file_role"])),
            snippet or "",
        ]
        if part
    )
    return SemanticChunk(
        chunk_id=f"symbol:{row['fqname']}",
        path=str(row["moodle_path"]),
        symbol=str(row["fqname"]),
        component=str(row["component_name"]),
        file_role=str(row["file_role"]),
        language="php",
        symbol_type=str(row["symbol_type"]),
        line=int(row["line"]) if row["line"] is not None else None,
        source_kind="symbol",
        title=title,
        summary=summary,
        text=text,
        snippet=snippet,
    )


def _semantic_chunk_from_js_row(row: sqlite3.Row) -> SemanticChunk:
    """Build one JS module chunk from a JS module row."""

    title = str(row["module_name"])
    summary = " ".join(
        part
        for part in [
            str(row["export_kind"] or "js_module"),
            str(row["export_name"] or ""),
            str(row["superclass_module"] or row["superclass_name"] or ""),
        ]
        if part
    ).strip()
    snippet = _source_snippet(str(row["absolute_path"]), None)
    text = " ".join(
        part
        for part in [
            row["module_name"],
            row["export_kind"] or "",
            row["export_name"] or "",
            row["superclass_name"] or "",
            row["superclass_module"] or "",
            row["build_file"] or "",
            row["moodle_path"],
            row["file_role"],
            row["component_name"],
            _semantic_role_tags(str(row["file_role"])),
            snippet or "",
        ]
        if part
    )
    return SemanticChunk(
        chunk_id=f"js:{row['module_name']}",
        path=str(row["moodle_path"]),
        symbol=str(row["module_name"]),
        component=str(row["component_name"]),
        file_role=str(row["file_role"]),
        language="js",
        symbol_type="js_module",
        line=None,
        source_kind="js_module",
        title=title,
        summary=summary,
        text=text,
        snippet=snippet,
    )


def _semantic_chunk_from_test_row(row: sqlite3.Row) -> SemanticChunk:
    """Build one test chunk from a test row."""

    snippet = _source_snippet(str(row["absolute_path"]), int(row["line"]) if row["line"] is not None else None)
    text = " ".join(
        part
        for part in [
            row["name"],
            row["test_type"],
            row["related_symbol"] or "",
            row["moodle_path"],
            row["file_role"],
            row["component_name"],
            "phpunit test coverage assertion fixture",
            snippet or "",
        ]
        if part
    )
    return SemanticChunk(
        chunk_id=f"test:{row['moodle_path']}:{row['name']}",
        path=str(row["moodle_path"]),
        symbol=str(row["related_symbol"]) if row["related_symbol"] else None,
        component=str(row["component_name"]),
        file_role=str(row["file_role"]),
        language="php",
        symbol_type="test",
        line=int(row["line"]) if row["line"] is not None else None,
        source_kind="test",
        title=str(row["name"]),
        summary=f"{row['test_type']} test",
        text=text,
        snippet=snippet,
    )


def _semantic_chunk_from_webservice_row(row: sqlite3.Row) -> SemanticChunk:
    """Build one service-definition chunk from a webservice row."""

    symbol = None
    if row["classname"] and row["methodname"]:
        symbol = f"{row['classname']}::{row['methodname']}"
    text = " ".join(
        part
        for part in [
            row["service_name"],
            row["classname"] or "",
            row["methodname"] or "",
            row["resolved_target_file"] or "",
            row["moodle_path"],
            row["file_role"],
            row["component_name"],
            "web service external api registration",
        ]
        if part
    )
    return SemanticChunk(
        chunk_id=f"service:{row['moodle_path']}:{row['service_name']}",
        path=str(row["moodle_path"]),
        symbol=symbol,
        component=str(row["component_name"]),
        file_role=str(row["file_role"]),
        language="php",
        symbol_type="service_definition",
        line=int(row["line"]) if row["line"] is not None else None,
        source_kind="service",
        title=str(row["service_name"]),
        summary="web service registration",
        text=text,
        snippet=str(row["service_name"]),
    )


def _semantic_chunk_from_file_row(connection: sqlite3.Connection, row: sqlite3.Row) -> SemanticChunk:
    """Build one file-level fallback chunk."""

    file_id = row["id"]
    symbols = connection.execute(
        """
        SELECT name
        FROM symbols
        WHERE file_id = ?
        ORDER BY line
        LIMIT 5
        """,
        (file_id,),
    ).fetchall()
    js_module = connection.execute(
        """
        SELECT module_name
        FROM js_modules
        WHERE file_id = ?
        LIMIT 1
        """,
        (file_id,),
    ).fetchone()
    services = connection.execute(
        """
        SELECT service_name
        FROM webservices
        WHERE file_id = ?
        ORDER BY line
        LIMIT 5
        """,
        (file_id,),
    ).fetchall()
    title = str(row["moodle_path"])
    summary = f"{row['file_role']} file"
    snippet = _source_snippet(str(row["absolute_path"]), None)
    text = " ".join(
        part
        for part in [
            row["moodle_path"],
            row["file_role"],
            row["component_name"],
            _semantic_role_tags(str(row["file_role"])),
            " ".join(str(item["name"]) for item in symbols),
            str(js_module["module_name"]) if js_module is not None else "",
            " ".join(str(item["service_name"]) for item in services),
            snippet or "",
        ]
        if part
    )
    return SemanticChunk(
        chunk_id=f"file:{row['moodle_path']}",
        path=str(row["moodle_path"]),
        symbol=None,
        component=str(row["component_name"]),
        file_role=str(row["file_role"]),
        language=_language_for_path(str(row["moodle_path"])),
        symbol_type=None,
        line=None,
        source_kind="file",
        title=title,
        summary=summary,
        text=text,
        snippet=snippet,
    )


def _semantic_like_clause(columns: list[str], query_tokens: list[str]) -> tuple[str, list[object]]:
    """Return one OR-based LIKE clause for a bounded token set."""

    filtered = query_tokens[:4]
    if not filtered:
        return "", []
    clauses: list[str] = []
    params: list[object] = []
    for token in filtered:
        pattern = f"%{token}%"
        for column in columns:
            clauses.append(f"lower({column}) LIKE ?")
            params.append(pattern)
    return " OR ".join(clauses), params


def _semantic_focus_tokens(text: str) -> list[str]:
    """Return a bounded list of informative tokens for hybrid retrieval."""

    counts = Counter(_semantic_tokens(text))
    ranked = sorted(counts, key=lambda token: (-counts[token], -len(token), token))
    return ranked[:6]


def _semantic_tokens(text: str) -> list[str]:
    """Return normalized tokens with light code-aware splitting."""

    prepared = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    raw = re.split(r"[^A-Za-z0-9_]+", prepared.lower())
    stopwords = {
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "from",
        "into",
        "file",
        "path",
        "class",
        "function",
        "method",
        "return",
        "public",
        "protected",
        "private",
        "null",
        "true",
        "false",
        "php",
        "js",
    }
    return [token for token in raw if len(token) >= 3 and token not in stopwords]


def _semantic_overlap_score(query_text: str, candidate_text: str) -> float:
    """Return a bounded lexical overlap score."""

    query_tokens = set(_semantic_focus_tokens(query_text))
    candidate_tokens = set(_semantic_tokens(candidate_text))
    if not query_tokens or not candidate_tokens:
        return 0.0
    return len(query_tokens & candidate_tokens) / len(query_tokens)


def _semantic_similarity(left_text: str, right_text: str) -> float:
    """Return cosine similarity between two hashed token vectors."""

    left = _semantic_vector(_semantic_tokens(left_text))
    right = _semantic_vector(_semantic_tokens(right_text))
    if not left or not right:
        return 0.0
    dot = sum(left[index] * right.get(index, 0.0) for index in left)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def _semantic_vector(tokens: list[str], dimensions: int = 96) -> dict[int, float]:
    """Return a deterministic sparse hashed vector for one token list."""

    counts = Counter(tokens)
    vector: dict[int, float] = {}
    for token, count in counts.items():
        digest = hashlib.md5(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        vector[index] = vector.get(index, 0.0) + float(count)
    return vector


def _source_snippet(absolute_path: str, line: int | None, radius: int = 2) -> str | None:
    """Return a small source snippet around one anchor line."""

    try:
        source = Path(absolute_path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    if not source:
        return None
    if line is None or line <= 0:
        snippet_lines = [item.strip() for item in source[: min(6, len(source))] if item.strip()]
        return " ".join(snippet_lines[:3]) or None
    start = max(0, line - 1 - radius)
    end = min(len(source), line + radius)
    snippet_lines = [item.strip() for item in source[start:end] if item.strip()]
    return " ".join(snippet_lines[:3]) or None


def _semantic_role_tags(file_role: str) -> str:
    """Return stable retrieval tags for one Moodle file role."""

    tags = {
        "external_api_class": "external api web service execute parameters returns",
        "services_definition": "db services registration external api entrypoint",
        "phpunit_test": "phpunit test coverage assertions",
        "output_class": "renderable output renderer template rendering",
        "renderer_file": "renderer rendering template output",
        "template_file": "mustache template rendering output context",
        "amd_source": "javascript amd source module import export frontend",
        "amd_build": "javascript built amd artifact generated",
        "settings_file": "admin settings framework configuration",
        "locallib_file": "feature entrypoint local business logic rendering",
    }
    return tags.get(file_role, file_role.replace("_", " "))


def _semantic_query_kind_bonus(query_tokens: list[str], chunk: SemanticChunk) -> float:
    """Return a small intent-aware bonus for free-text retrieval."""

    token_set = set(query_tokens)
    bonus = 0.0
    if {"method", "methods", "api", "external"} & token_set:
        if chunk.file_role == "external_api_class":
            bonus += 0.14
        elif chunk.file_role == "services_definition" or chunk.source_kind == "service":
            bonus += 0.12
        elif chunk.source_kind == "symbol":
            bonus += 0.06
    if {"test", "tests", "phpunit", "coverage"} & token_set:
        if chunk.source_kind == "test" and "/tests/external/" in chunk.path:
            bonus += 0.16
        elif chunk.source_kind == "test":
            bonus += 0.06
    if {"template", "renderer", "rendering"} & token_set and chunk.file_role in {"template_file", "renderer_file", "output_class"}:
        bonus += 0.06
    if {"form", "forms", "validation"} & token_set and chunk.file_role in {"unknown", "phpunit_test"} and "\\form\\" in (chunk.symbol or ""):
        bonus += 0.04
    return bonus


def _semantic_query_intent(query_tokens: list[str]) -> dict[str, bool]:
    """Return a small intent profile for bounded free-text ranking."""

    token_set = set(query_tokens)
    return {
        "external_api": bool({"external", "api", "service", "services", "webservice", "webservices"} & token_set),
        "tests": bool({"test", "tests", "phpunit", "coverage"} & token_set),
        "examples": bool({"examples", "example", "similar", "pattern"} & token_set),
        "rendering": bool({"rendering", "renderer", "template", "output"} & token_set),
        "forms": bool({"form", "forms", "validation"} & token_set),
    }


def _semantic_query_intent_multiplier(
    intent: dict[str, bool],
    chunk: SemanticChunk,
) -> float:
    """Return a small intent-aware multiplier for free-text retrieval.

    This keeps free-text queries grounded in trusted structural priors instead
    of letting broad lexical overlap dominate the ranking.
    """

    multiplier = 1.0
    if intent["external_api"] and intent["tests"]:
        if chunk.file_role == "external_api_class":
            multiplier *= 1.5
        elif chunk.file_role == "services_definition" or chunk.source_kind == "service":
            multiplier *= 1.35
        elif chunk.source_kind == "test" and "/tests/external/" in chunk.path:
            multiplier *= 1.18
        elif chunk.source_kind == "test":
            multiplier *= 0.7
        elif chunk.path.startswith("admin/tool/componentlibrary/"):
            multiplier *= 0.7
        elif chunk.file_role not in {"external_api_class", "services_definition"}:
            multiplier *= 0.9
    if intent["rendering"] and chunk.file_role in {"output_class", "renderer_file", "template_file"}:
        multiplier *= 1.15
    if intent["forms"] and chunk.file_role in {"form_class", "phpunit_test"} and "\\form\\" in (chunk.symbol or ""):
        multiplier *= 1.12
    if intent["examples"] and chunk.source_kind in {"symbol", "service", "test"}:
        multiplier *= 1.05
    return multiplier


def _language_for_path(path: str) -> str:
    """Return a coarse language bucket for one Moodle path."""

    if path.endswith(".js"):
        return "js"
    if path.endswith(".mustache"):
        return "template"
    return "php"


def _semantic_human_kind(chunk: SemanticChunk) -> str:
    """Return a small human-readable kind label."""

    if chunk.source_kind == "js_module":
        return "JavaScript module"
    if chunk.source_kind == "test":
        return "test file"
    if chunk.source_kind == "service":
        return "service definition"
    if chunk.source_kind == "symbol":
        return "definition"
    return "file"


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
            f.file_role,
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


def _find_js_module_definitions(
    connection: sqlite3.Connection,
    symbol_query: str,
    limit: int,
) -> list[JsDefinitionCandidate]:
    """Return JS module definitions for Moodle AMD source module queries."""

    rows = connection.execute(
        """
        SELECT
            jm.*,
            f.repository_relative_path,
            f.moodle_path,
            f.file_role,
            c.name AS component_name
        FROM js_modules jm
        JOIN files f ON f.id = jm.file_id
        JOIN components c ON c.id = jm.component_id
        WHERE jm.module_name = ?
           OR f.moodle_path = ?
           OR jm.build_file = ?
        ORDER BY
            CASE
                WHEN jm.module_name = ? THEN 0
                WHEN f.moodle_path = ? THEN 1
                WHEN jm.build_file = ? THEN 2
                ELSE 3
            END,
            jm.module_name
        LIMIT ?
        """,
        (
            symbol_query,
            symbol_query,
            symbol_query,
            symbol_query,
            symbol_query,
            symbol_query,
            limit,
        ),
    ).fetchall()
    return [JsDefinitionCandidate(row=item) for item in rows]


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
            f.file_role,
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
    linked_artifacts = _build_definition_linked_artifacts(connection, row)
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
        "linked_artifacts": linked_artifacts,
    }


def _serialize_js_definition_match(connection: sqlite3.Connection, candidate: JsDefinitionCandidate) -> dict:
    """Return an IDE-style definition payload for one indexed JS module."""

    row = candidate.row
    imports = connection.execute(
        """
        SELECT module_name, line, import_kind, imported_name, local_name, resolved_target_file, resolution_status
        FROM js_imports
        WHERE js_module_id = ?
        ORDER BY line, module_name, local_name
        """,
        (row["id"],),
    ).fetchall()
    js_imports = [_serialize_js_import(connection, item) for item in imports]
    js_navigation = _build_js_navigation_artifacts(connection, row, js_imports)
    return {
        "symbol_type": "js_module",
        "name": row["module_name"].split("/", 1)[-1],
        "fqname": row["module_name"],
        "module_name": row["module_name"],
        "component": row["component_name"],
        "file": row["moodle_path"],
        "repository_relative_path": row["repository_relative_path"],
        "line": 1,
        "namespace": None,
        "class_name": None,
        "signature": None,
        "parameters": [],
        "return_type": None,
        "docblock_summary": None,
        "docblock_tags": [],
        "visibility": None,
        "is_static": False,
        "is_final": False,
        "is_abstract": False,
        "matched_via": candidate.matched_via,
        "requested_class_name": None,
        "inheritance_role": "base_definition" if not row["superclass_module"] else "override",
        "overrides": row["superclass_module"],
        "implements_method": [],
        "parent_class": row["superclass_module"],
        "interface_names": [],
        "parent_definition": js_navigation.get("superclass"),
        "overrides_definition": js_navigation.get("superclass"),
        "implements_definitions": [],
        "child_overrides": js_navigation.get("imported_by", []),
        "export_kind": row["export_kind"],
        "export_name": row["export_name"],
        "superclass_name": row["superclass_name"],
        "superclass_module": row["superclass_module"],
        "resolved_superclass_file": js_navigation.get("superclass", {}).get("file")
        if js_navigation.get("superclass")
        else row["resolved_superclass_file"],
        "build_file": row["build_file"],
        "build_status": row["build_status"],
        "imports": js_imports,
        "linked_artifacts": {
            "javascript": js_navigation,
        },
    }


def _build_definition_linked_artifacts(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, object]:
    """Return bounded linked artifacts for one definition's owning file."""

    file_row = {
        "id": row["file_id"],
        "moodle_path": row["moodle_path"],
        "file_role": row["file_role"],
    }
    relationships = connection.execute(
        """
        SELECT source_fqname, target_name, relationship_type, line
        FROM relationships
        WHERE file_id = ?
        ORDER BY line, relationship_type, source_fqname, target_name
        """,
        (row["file_id"],),
    ).fetchall()
    class_references = _linked_class_artifacts(connection, relationships)
    rendering_artifacts = _build_rendering_linked_artifacts(connection, file_row, class_references)
    service_artifacts = _service_artifacts_for_definition_file(connection, row["file_id"], row["moodle_path"])
    entrypoint_links = _build_entrypoint_links(
        connection,
        file_row,
        service_artifacts,
        rendering_artifacts,
        None,
    )
    return {
        "services": service_artifacts,
        "rendering": rendering_artifacts,
        "entrypoints": entrypoint_links,
    }


def _compact_match_summary(match: dict[str, object]) -> dict[str, object]:
    """Return a compact anchor summary for Phase 4A navigation responses."""

    summary = {
        "symbol_type": match.get("symbol_type"),
        "name": match.get("name"),
        "fqname": match.get("fqname"),
        "file": match.get("file"),
        "component": match.get("component"),
        "line": match.get("line"),
    }
    if match.get("module_name"):
        summary["module_name"] = match.get("module_name")
    return summary


def _related_items_for_symbol_results(
    connection: sqlite3.Connection,
    matches: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return related-definition items around resolved symbol matches."""

    def _related_target(item: dict[str, object]) -> tuple[str | None, str | None]:
        """Return path/symbol for PHP definitions or JS module summaries."""

        path = item.get("file") or item.get("path")
        symbol = item.get("fqname") or item.get("module_name")
        if path is None:
            return None, None
        return str(path), str(symbol) if symbol else None

    items: list[dict[str, object]] = []
    for match in matches:
        anchor = match.get("fqname") or match.get("module_name") or match.get("file")
        symbol_type = str(match.get("symbol_type") or "")
        is_js_module = symbol_type == "js_module"

        if not is_js_module and match.get("parent_definition"):
            parent_path, parent_symbol = _related_target(match["parent_definition"])
            if parent_path:
                items.append(
                    _navigation_item(
                        item_type="definition",
                        relationship="parent_definition",
                        confidence="high",
                        reason="because this definition inherits from or extends this parent/base definition",
                        path=parent_path,
                        symbol=parent_symbol,
                        anchor=anchor,
                    )
                )
        if not is_js_module and match.get("overrides_definition"):
            override_path, override_symbol = _related_target(match["overrides_definition"])
            if override_path:
                items.append(
                    _navigation_item(
                        item_type="definition",
                        relationship="overrides_definition",
                        confidence="high",
                        reason="because this definition overrides this base method",
                        path=override_path,
                        symbol=override_symbol,
                        anchor=anchor,
                    )
                )
        for implemented in ([] if is_js_module else match.get("implements_definitions", [])):
            implemented_path, implemented_symbol = _related_target(implemented)
            if not implemented_path:
                continue
            items.append(
                _navigation_item(
                    item_type="definition",
                    relationship="implements_definition",
                    confidence="high",
                    reason="because this definition implements this interface method",
                    path=implemented_path,
                    symbol=implemented_symbol,
                    anchor=anchor,
                )
            )
        for child in ([] if is_js_module else match.get("child_overrides", [])[:4]):
            child_path, child_symbol = _related_target(child)
            if not child_path:
                continue
            items.append(
                _navigation_item(
                    item_type="definition",
                    relationship="child_override",
                    confidence="medium",
                    reason="because this definition is overridden by this child implementation",
                    path=child_path,
                    symbol=child_symbol,
                    anchor=anchor,
                )
            )
        items.extend(
            _artifact_navigation_items(
                match.get("linked_artifacts", {}),
                anchor=anchor,
                include_anchor_file=False,
                include_entrypoints=False,
                include_js_reverse=not is_js_module,
            )
        )
    return items


def _edit_surface_items_for_symbol_results(
    connection: sqlite3.Connection,
    matches: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return likely edit-surface items around resolved symbol matches."""

    items: list[dict[str, object]] = []
    for match in matches:
        anchor = match.get("fqname") or match.get("module_name") or match.get("file")
        is_js_module = str(match.get("symbol_type") or "") == "js_module"
        items.append(
            _navigation_item(
                item_type="definition_file",
                relationship="definition_file",
                confidence="high",
                reason="because this is the defining file for the queried symbol",
                path=str(match["file"]),
                symbol=str(match.get("fqname") or match.get("module_name") or ""),
                anchor=anchor,
            )
        )
        items.extend(
            _artifact_navigation_items(
                match.get("linked_artifacts", {}),
                anchor=anchor,
                include_anchor_file=False,
                include_entrypoints=False,
                include_js_reverse=not is_js_module,
            )
        )
    return items


def _related_items_for_file_context(context: dict[str, object]) -> list[dict[str, object]]:
    """Return related-definition items around one indexed file context."""

    anchor = str(context["moodle_path"])
    items = _artifact_navigation_items(
        context.get("linked_artifacts", {}),
        anchor=anchor,
        include_anchor_file=False,
        include_entrypoints=True,
        include_js_reverse=True,
    )
    for symbol in list(context.get("symbols", []))[:4]:
        fqname = symbol.get("fqname")
        if not fqname:
            continue
        items.append(
            _navigation_item(
                item_type="definition",
                relationship="defines_symbol",
                confidence="high",
                reason="because this file directly defines this symbol",
                path=str(context["moodle_path"]),
                symbol=str(fqname),
                anchor=anchor,
            )
        )
    return items


def _edit_surface_items_for_file_context(
    context: dict[str, object],
    related: dict[str, object],
) -> list[dict[str, object]]:
    """Return likely edit-surface items around one indexed file.

    File-driven edit-surface results should start with the anchor file itself,
    then move outward to the next files a user is likely to edit. We therefore
    drop linked-artifact items that point back to the anchor file, because they
    are already represented by the explicit ``anchor_file`` item and otherwise
    crowd out the real next-hop artifacts.
    """

    anchor = str(context["moodle_path"])
    items = [
        _navigation_item(
            item_type="file",
            relationship="anchor_file",
            confidence="high",
            reason="because this is the file you are considering changing",
            path=anchor,
            symbol=None,
            anchor=anchor,
        )
    ]
    items.extend(
        item
        for item in _artifact_navigation_items(
            context.get("linked_artifacts", {}),
            anchor=anchor,
            include_anchor_file=False,
            include_entrypoints=True,
            include_js_reverse=True,
        )
        if str(item["path"]) != anchor
    )
    for suggestion in related.get("suggestions", []):
        path = str(suggestion["path"])
        if path == anchor:
            continue
        confidence = _suggestion_confidence(path, str(suggestion["reason"]))
        items.append(
            _navigation_item(
                item_type=_artifact_item_type(path, None),
                relationship="suggested_companion",
                confidence=confidence,
                reason=str(suggestion["reason"]),
                path=path,
                symbol=None,
                anchor=anchor,
            )
        )
    return items


def _dependency_sections_for_symbol_results(
    matches: list[dict[str, object]],
    *,
    limit: int,
) -> dict[str, object]:
    """Return bounded dependency sections for symbol-driven workflows."""

    sections: dict[str, list[dict[str, object]]] = {
        "likely_callers": [],
        "likely_callees": [],
        "linked_tests": [],
        "linked_services": [],
        "linked_rendering_artifacts": [],
        "linked_forms": [],
        "linked_framework": [],
        "linked_javascript": [],
        "linked_build_artifacts": [],
    }

    for match in matches:
        # Dependency-neighborhood ranking is path-aware, so symbol-driven
        # neighborhoods should anchor on the owning file whenever we have one.
        # Falling back to fqname/module_name is still fine for symbols without a
        # concrete file anchor, but using the file keeps same-component
        # rendering/form chains ordered like real "inspect next" steps.
        anchor = str(match.get("file") or match.get("fqname") or match.get("module_name") or "")
        symbol_type = str(match.get("symbol_type") or "")

        for usage in match.get("usage_examples", []):
            item = _dependency_item_from_usage(usage, anchor=anchor)
            if item is not None:
                if str(item["relationship"]) == "test_usage":
                    sections["linked_tests"].append(item)
                else:
                    sections["likely_callers"].append(item)

        linked_artifacts = match.get("linked_artifacts", {})
        _extend_dependency_sections_from_linked_artifacts(
            sections,
            linked_artifacts,
            anchor=anchor,
            include_js_importers=symbol_type != "js_module",
            include_entrypoints=False,
            owning_file=str(match.get("file") or ""),
            execution_mode="symbol",
        )

    return _finalize_dependency_sections(sections, limit=limit)


def _dependency_sections_for_file_context(
    context: dict[str, object],
    *,
    limit: int,
) -> dict[str, object]:
    """Return bounded dependency sections for file-driven workflows."""

    anchor = str(context["moodle_path"])
    sections: dict[str, list[dict[str, object]]] = {
        "likely_callers": [],
        "likely_callees": [],
        "linked_tests": [],
        "linked_services": [],
        "linked_rendering_artifacts": [],
        "linked_forms": [],
        "linked_framework": [],
        "linked_javascript": [],
        "linked_build_artifacts": [],
    }

    for test in context.get("tests", []):
        test_file = test.get("file")
        if not test_file:
            continue
        sections["linked_tests"].append(
            _dependency_item(
                item_type="service_test",
                relationship="linked_test",
                confidence="high",
                reason=str(test.get("reason") or "because this concrete test file is directly linked to the queried file"),
                path=str(test_file),
                symbol=None,
                anchor=anchor,
                line=test.get("line"),
            )
        )

    _extend_dependency_sections_from_linked_artifacts(
        sections,
        context.get("linked_artifacts", {}),
        anchor=anchor,
        include_js_importers=True,
        include_entrypoints=True,
        owning_file=anchor,
        execution_mode="file",
    )

    return _finalize_dependency_sections(sections, limit=limit)


def _extend_dependency_sections_from_linked_artifacts(
    sections: dict[str, list[dict[str, object]]],
    linked_artifacts: dict[str, object],
    *,
    anchor: str,
    include_js_importers: bool,
    include_entrypoints: bool,
    owning_file: str,
    execution_mode: str,
) -> None:
    """Project trusted linked-artifact structures into dependency sections."""

    for service in linked_artifacts.get("services", []) or []:
        service_name = str(service["service_name"])
        source_file = service.get("source_file")
        if source_file:
            service_item = _dependency_item(
                item_type="service_definition",
                relationship="service_definition",
                confidence="high",
                reason=f"because this implementation is registered by service {service_name} in db/services.php",
                path=str(source_file),
                symbol=service_name,
                anchor=anchor,
            )
            sections["linked_services"].append(service_item)
            sections["likely_callers"].append(service_item)
        implementation_file = service.get("implementation_file")
        if implementation_file and str(implementation_file) != owning_file:
            implementation_item = _dependency_item(
                item_type="service_implementation",
                relationship="service_implementation",
                confidence="high",
                reason=f"because service {service_name} resolves to this implementation file",
                path=str(implementation_file),
                symbol=None,
                anchor=anchor,
            )
            sections["linked_services"].append(implementation_item)
            sections["likely_callees"].append(implementation_item)
        for test in service.get("related_tests", []):
            sections["linked_tests"].append(
                _dependency_item(
                    item_type="service_test",
                    relationship="service_test",
                    confidence="high",
                    reason=str(test["reason"]),
                    path=str(test["file"]),
                    symbol=None,
                    anchor=anchor,
                )
            )

    for rendering in linked_artifacts.get("rendering", []) or []:
        for item in _dependency_rendering_items(rendering, anchor=anchor):
            if item["type"] in {"output_class", "renderer_file", "template_file"}:
                sections["linked_rendering_artifacts"].append(item)
                continue
            if item["type"] == "form_class":
                sections["linked_forms"].append(item)
                if (
                    execution_mode == "symbol"
                    and str(item.get("chain_role", "direct")) == "direct"
                    and _is_direct_form_callee(item)
                ):
                    sections["likely_callees"].append(item)
                continue
            if item["type"] in {"framework_base", "class_file"}:
                sections["linked_framework"].append(item)
                continue

    javascript = linked_artifacts.get("javascript")
    if isinstance(javascript, dict):
        for item in javascript.get("imports", []) or []:
            if not item.get("file"):
                continue
            dependency_item = _dependency_item(
                item_type="js_module",
                relationship="js_import",
                confidence="high",
                reason=f"because this JavaScript module imports {item['module_name']}",
                path=str(item["file"]),
                symbol=str(item["module_name"]),
                anchor=anchor,
            )
            sections["likely_callees"].append(dependency_item)
            sections["linked_javascript"].append(dependency_item)
        superclass = javascript.get("superclass")
        if isinstance(superclass, dict) and superclass.get("file"):
            dependency_item = _dependency_item(
                item_type="js_module",
                relationship="js_superclass",
                confidence="high",
                reason=str(superclass["reason"]),
                path=str(superclass["file"]),
                symbol=str(superclass.get("module_name") or ""),
                anchor=anchor,
            )
            sections["likely_callees"].append(dependency_item)
            sections["linked_javascript"].append(dependency_item)
        build_artifact = javascript.get("build_artifact")
        if isinstance(build_artifact, dict):
            sections["linked_build_artifacts"].append(
                _dependency_item(
                    item_type="js_build_artifact",
                    relationship="js_build_artifact",
                    confidence="high",
                    reason=str(build_artifact["reason"]),
                    path=str(build_artifact["path"]),
                    symbol=None,
                    anchor=anchor,
                )
            )
        if include_js_importers:
            for importer in javascript.get("imported_by", [])[:5]:
                sections["likely_callers"].append(
                    _dependency_item(
                        item_type="js_module",
                        relationship="js_imported_by",
                        confidence="high",
                        reason=str(importer["reason"]),
                        path=str(importer["file"]),
                        symbol=str(importer["module_name"]),
                        anchor=anchor,
                        line=importer.get("line"),
                    )
                )

    if include_entrypoints:
        for entrypoint in linked_artifacts.get("entrypoints", []) or []:
            item = _dependency_item(
                item_type=_artifact_item_type(str(entrypoint["path"]), str(entrypoint.get("artifact_type"))),
                relationship=str(entrypoint.get("artifact_type") or "entrypoint_link"),
                confidence=_artifact_confidence(str(entrypoint.get("artifact_type")), "supporting"),
                reason=str(entrypoint["reason"]),
                path=str(entrypoint["path"]),
                symbol=None,
                anchor=anchor,
            )
            if item["type"] in {"output_class", "renderer_file", "template_file"}:
                sections["linked_rendering_artifacts"].append(item)
            elif item["type"] == "form_class":
                sections["linked_forms"].append(item)
            elif item["type"] in {"framework_base", "class_file"}:
                sections["linked_framework"].append(item)
            elif item["type"] in {"service_definition", "service_implementation"}:
                sections["linked_services"].append(item)
            elif item["type"] == "service_test":
                sections["linked_tests"].append(item)


def _dependency_rendering_items(node: dict[str, object], *, anchor: str) -> list[dict[str, object]]:
    """Return flattened dependency items for a rendering/form artifact chain."""

    path = str(node["path"])
    item_type = _artifact_item_type(path, str(node.get("artifact_type")))
    symbol = str(node.get("class_name") or "") or None
    reason = str(node["reason"])
    if item_type == "form_class" and symbol:
        if str(node.get("chain_role", "direct")) == "direct":
            reason = f"because the queried code directly references or instantiates form class {symbol}"
        else:
            reason = f"because this is an intermediate form class in the resolved form inheritance chain for {symbol}"
    elif item_type == "framework_base" and path == "lib/formslib.php":
        reason = "because this is the Moodle form framework base reached through the resolved form inheritance chain"
    elif item_type == "class_file" and symbol:
        reason = f"because this is the base class implementation for {symbol}"

    items = [
        _dependency_item(
            item_type=item_type,
            relationship=str(node.get("artifact_type") or "linked_artifact"),
            confidence=_artifact_confidence(str(node.get("artifact_type")), str(node.get("chain_role", "direct"))),
            reason=reason,
            path=path,
            symbol=symbol,
            anchor=anchor,
            chain_role=str(node.get("chain_role", "direct")),
            has_next_hops=bool(node.get("next_hops")),
        )
    ]
    for hop in node.get("next_hops", []) or []:
        items.extend(_dependency_rendering_items(hop, anchor=anchor))
    return items


def _dependency_item_from_usage(
    usage: dict[str, object],
    *,
    anchor: str,
) -> dict[str, object] | None:
    """Translate one high-confidence usage example into a dependency edge."""

    usage_kind = str(usage["usage_kind"])
    path = str(usage["file"])
    confidence = str(usage["confidence"])
    line = usage.get("line")
    snippet = str(usage.get("snippet") or "").strip()
    item_type = "service_test" if _is_concrete_test_path(path) else _artifact_item_type(path, None)

    reason_map = {
        "service_definition": "because this method is registered in db/services.php",
        "test_usage": "because this concrete PHPUnit file appears to exercise the queried symbol directly",
        "renderer_usage": "because this renderer directly calls or renders the queried symbol",
        "form_usage": "because this form code directly calls or instantiates the queried symbol",
        "static_method_call": "because this file contains a direct static call to the queried symbol",
        "instance_method_call": "because this file contains a high-confidence instance call to the queried symbol",
        "function_call": "because this file contains a direct function call to the queried symbol",
        "js_import_usage": "because this module directly imports the queried JavaScript module",
        "js_superclass_usage": "because this module subclasses the queried JavaScript module",
    }
    reason = reason_map.get(usage_kind)
    if reason is None:
        return None
    if snippet:
        reason = f"{reason}: {snippet}"
    return _dependency_item(
        item_type=item_type,
        relationship=usage_kind,
        confidence=confidence,
        reason=reason,
        path=path,
        symbol=None,
        anchor=anchor,
        line=line,
    )


def _dependency_item(
    *,
    item_type: str,
    relationship: str,
    confidence: str,
    reason: str,
    path: str,
    symbol: str | None,
    anchor: str,
    line: int | None = None,
    chain_role: str | None = None,
    has_next_hops: bool | None = None,
) -> dict[str, object]:
    """Return a normalized dependency-neighborhood item."""

    item = {
        "type": item_type,
        "relationship": relationship,
        "confidence": confidence,
        "reason": reason,
        "path": path,
        "symbol": symbol,
        "anchor": anchor,
    }
    if line is not None:
        item["line"] = line
    if chain_role is not None:
        item["chain_role"] = chain_role
    if has_next_hops is not None:
        item["has_next_hops"] = has_next_hops
    return item


def _same_component_root(anchor: str, path: str) -> bool:
    """Return whether two Moodle paths appear to belong to the same component root."""

    def component_root(candidate: str) -> str:
        parts = [part for part in candidate.split("/") if part]
        if len(parts) >= 3 and parts[:2] == ["admin", "tool"]:
            return "/".join(parts[:3])
        if len(parts) >= 3 and parts[:2] == ["ai", "provider"]:
            return "/".join(parts[:3])
        if len(parts) >= 2 and parts[0] in {"mod", "block", "local", "theme", "enrol", "report", "question", "course"}:
            return "/".join(parts[:2])
        return parts[0] if parts else candidate

    return component_root(anchor) == component_root(path)


def _is_direct_form_callee(item: dict[str, object]) -> bool:
    """Return whether a direct form link should behave like an execution callee.

    Provider neighborhoods often include both concrete forms and shared form
    bases. Only concrete forms belong in ``likely_callees``; shared bases stay
    in ``linked_forms``/``linked_framework`` so the neighborhood reads like an
    actionable chain instead of a mixed bag of instantiations and inheritance.
    """

    symbol = str(item.get("symbol") or "")
    if "\\" in symbol:
        return True
    return bool(item.get("has_next_hops"))


def _finalize_dependency_sections(
    sections: dict[str, list[dict[str, object]]],
    *,
    limit: int,
) -> dict[str, object]:
    """Deduplicate, rank, decorate, and bound dependency sections.

    This stays intentionally local: we score only the already trusted
    relationships in each bounded section, then derive a tiny cross-section
    ``primary_focus`` list for agent workflows.
    """

    finalized_sections: dict[str, dict[str, object]] = {}
    focus_candidates: list[dict[str, object]] = []
    for section_name, items in sections.items():
        section_limit = min(limit, 6)
        merged = _merge_dependency_section_items(items, section_name=section_name, limit=section_limit)
        if merged:
            decorated = [_present_dependency_section_item(section_name, item) for item in merged]
            decorated = _calibrate_dependency_section_scores(section_name, decorated)
            decorated = _prune_dependency_section_items(section_name, decorated, limit=section_limit)
            if not decorated:
                continue
            finalized_sections[section_name] = {
                "summary": _dependency_section_summary(section_name),
                "items": decorated,
            }
            focus_candidates.extend(
                [{**item, "_section": section_name} for item in decorated]
            )
    _prune_generic_service_framework_sections(finalized_sections)
    return {
        "primary_focus": _dependency_primary_focus(focus_candidates),
        "sections": finalized_sections,
    }


def _prune_generic_service_framework_sections(
    finalized_sections: dict[str, dict[str, object]],
) -> None:
    """Drop generic framework residue from service slices.

    Service neighborhoods are most useful when they stay anchored on the
    concrete API flow: registration, implementation, and tests. A generic
    inherited base such as ``external_api`` can still be structurally correct,
    but if that is the only remaining framework companion it adds more noise
    than guidance. Keep richer framework sections for form-driven slices.
    """

    if "linked_services" not in finalized_sections or "linked_framework" not in finalized_sections:
        return

    framework_items = list(finalized_sections["linked_framework"].get("items", []))
    if not framework_items:
        finalized_sections.pop("linked_framework", None)
        return

    only_generic_class_files = all(
        str(item.get("type")) == "class_file" and str(item.get("path")) == "lib/externallib.php"
        for item in framework_items
    )
    if only_generic_class_files:
        finalized_sections.pop("linked_framework", None)


def _plan_profile_for_symbol(match: dict[str, object]) -> dict[str, object]:
    """Return a lightweight planning profile for one symbol anchor."""

    path = str(match.get("file") or "")
    linked_artifacts = match.get("linked_artifacts", {}) or {}
    return {
        "anchor_path": path,
        "anchor_symbol": str(match.get("fqname") or match.get("module_name") or ""),
        "anchor_type": str(match.get("symbol_type") or ""),
        "anchor_file_role": _file_role_for_plan_path(path),
        "service": bool(linked_artifacts.get("services")) or _file_role_for_plan_path(path) in {"external_api_class", "services_definition"},
        "rendering": bool(linked_artifacts.get("rendering")) or _file_role_for_plan_path(path) in {"locallib_file", "output_class", "renderer_file", "template_file"},
        "provider_form": "provider" in path and "/form/" not in path,
        "js": str(match.get("symbol_type") or "") == "js_module" or "/amd/src/" in path,
        "query_intent": {},
    }


def _plan_profile_for_file(context: dict[str, object]) -> dict[str, object]:
    """Return a lightweight planning profile for one file anchor."""

    path = str(context["moodle_path"])
    file_role = str(context.get("file_role") or _file_role_for_plan_path(path))
    linked_artifacts = context.get("linked_artifacts", {}) or {}
    return {
        "anchor_path": path,
        "anchor_symbol": None,
        "anchor_type": "file",
        "anchor_file_role": file_role,
        "service": file_role == "services_definition" or bool(linked_artifacts.get("services")),
        "rendering": file_role in {"locallib_file", "output_class", "renderer_file", "template_file"} or bool(linked_artifacts.get("rendering")),
        "provider_form": "provider" in path,
        "js": file_role == "amd_source" or "/amd/src/" in path,
        "query_intent": {},
    }


def _plan_profile_for_query(query_text: str) -> dict[str, object]:
    """Return a lightweight planning profile for one free-text change goal."""

    tokens = _semantic_focus_tokens(query_text)
    intent = _semantic_query_intent(tokens)
    return {
        "anchor_path": "",
        "anchor_symbol": None,
        "anchor_type": "query",
        "anchor_file_role": "",
        "service": intent["external_api"],
        "rendering": intent["rendering"],
        "provider_form": intent["forms"],
        "js": bool({"javascript", "js", "amd", "module"} & set(tokens)),
        "query_intent": intent,
    }


def _anchor_change_reason(profile: dict[str, object]) -> str:
    """Return the anchor-file explanation for change planning."""

    anchor_symbol = str(profile.get("anchor_symbol") or "")
    anchor_path = str(profile.get("anchor_path") or "")
    if anchor_symbol:
        return f"Defines the queried symbol {anchor_symbol}; inspect and update this implementation first."
    return f"Defines the queried file anchor {anchor_path}; inspect and update this file first."


def _change_plan_candidate(
    *,
    path: str,
    symbol: str | None,
    confidence: str,
    reason: str,
    profile: dict[str, object],
    relationship: str,
    item_type: str,
    source_rank: int,
) -> dict[str, object] | None:
    """Classify one trusted navigation item into a plan candidate."""

    if not path:
        return None
    bucket, change_role = _classify_change_plan_item(
        path=path,
        symbol=symbol,
        relationship=relationship,
        item_type=item_type,
        profile=profile,
    )
    if bucket is None or change_role is None:
        return None
    return {
        "path": path,
        "symbol": symbol,
        "change_role": change_role,
        "confidence": confidence,
        "reason": _change_plan_reason(
            reason,
            change_role=change_role,
            relationship=relationship,
            path=path,
            profile=profile,
        ),
        "bucket": bucket,
        "_source_rank": source_rank,
    }


def _classify_change_plan_item(
    *,
    path: str,
    symbol: str | None,
    relationship: str,
    item_type: str,
    profile: dict[str, object],
) -> tuple[str | None, str | None]:
    """Return required/likely/optional and one stable change role."""

    anchor_path = str(profile.get("anchor_path") or "")
    service = bool(profile.get("service"))
    rendering = bool(profile.get("rendering"))
    provider_form = bool(profile.get("provider_form"))
    js = bool(profile.get("js"))
    anchor_type = str(profile.get("anchor_type") or "")
    intent = dict(profile.get("query_intent") or {})
    file_role = _file_role_for_plan_path(path)

    if path == anchor_path or relationship == "anchor_definition":
        return "required", "implementation"

    if item_type in {"service_implementation", "definition_file"} or relationship in {"definition_file", "service_implementation"}:
        return "required", "implementation"

    if item_type == "service_definition" or relationship == "service_definition" or path.endswith("/db/services.php"):
        if anchor_type == "query":
            return ("likely" if intent.get("external_api") else "optional"), "entrypoint"
        return ("required" if service or intent.get("external_api") else "likely"), "entrypoint"

    if item_type == "service_test" or "test" in relationship or path.endswith("_test.php") or path.endswith("_advanced_testcase.php"):
        return ("required" if service or intent.get("tests") else "likely"), "validation"

    if item_type == "output_class":
        return ("likely" if rendering else "optional"), "rendering_companion"
    if item_type == "renderer_file":
        return ("likely" if rendering else "optional"), "rendering_companion"
    if item_type == "template_file":
        return "optional", "rendering_companion"

    if item_type == "form_class":
        if provider_form and "/provider/" in path:
            if path.endswith("/action_form.php"):
                return "likely", "form_companion"
            return "required", "form_companion"
        if provider_form or intent.get("forms"):
            return "likely", "form_companion"
        return "optional", "form_companion"

    if item_type in {"framework_base", "class_file"} or file_role in {"lib_file", "settings_file"}:
        if provider_form or rendering:
            return "likely", "framework_context"
        return "optional", "framework_context"

    if item_type == "js_build_artifact" or "/amd/build/" in path:
        return "optional", "build_artifact"

    if item_type == "js_module" or "/amd/src/" in path:
        if js:
            return "likely", "implementation"
        return "optional", "example_reference"

    if intent.get("external_api") and ("/classes/external/" in path or path.endswith("/externallib.php")):
        return "required", "implementation"

    if relationship.startswith("semantic_"):
        return "optional", "example_reference"
    if service:
        return "likely", "framework_context"
    if rendering or provider_form or js:
        return "optional", "example_reference"
    return None, None


def _change_plan_reason(
    existing_reason: str,
    *,
    change_role: str,
    relationship: str,
    path: str,
    profile: dict[str, object],
) -> str:
    """Return a decision-ready reason for one plan target."""

    anchor_symbol = str(profile.get("anchor_symbol") or "")
    anchor_label = anchor_symbol or str(profile.get("anchor_path") or "this change")
    if change_role == "implementation":
        if relationship in {"anchor_definition", "definition_file", "service_implementation"}:
            return f"Defines the implementation for {anchor_label}; update this file first when behavior, parameters, or return shape changes."
        if "/amd/src/" in path:
            return f"Defines a directly linked source module for {anchor_label}; inspect it if dependency contracts or inherited client-side behavior must change."
        return existing_reason or f"Implements behavior directly tied to {anchor_label}; inspect it when the core logic changes."
    if change_role == "entrypoint":
        return f"Registers or exposes {anchor_label} as an entrypoint; update it if signatures, routing, or service registration details change."
    if change_role == "validation":
        return f"Validates expected behavior around {anchor_label}; update this test or verification surface when outputs, parameters, or side effects change."
    if change_role == "rendering_companion":
        return f"Participates in the rendering flow around {anchor_label}; inspect it if rendered data, renderer logic, or template structure changes."
    if change_role == "form_companion":
        return f"Defines a concrete or inherited form used around {anchor_label}; update it if fields, defaults, or validation rules change."
    if change_role == "framework_context":
        return f"Provides framework or base-class context for {anchor_label}; inspect it if the change touches shared contracts or inherited behavior."
    if change_role == "build_artifact":
        return f"Generated build artifact for {anchor_label}; regenerate it only if the workflow commits built assets."
    return existing_reason or f"Provides a comparable supporting example for {anchor_label}; inspect it if you need a reference implementation."


def _merge_change_plan_candidates(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    """Deduplicate and keep the strongest plan classification for each path."""

    merged: dict[str, dict[str, object]] = {}
    for item in candidates:
        path = str(item["path"])
        existing = merged.get(path)
        if existing is None:
            merged[path] = dict(item)
            continue
        if _change_bucket_rank(str(item["bucket"])) < _change_bucket_rank(str(existing["bucket"])):
            existing["bucket"] = item["bucket"]
            existing["change_role"] = item["change_role"]
            existing["reason"] = item["reason"]
        elif (
            _change_bucket_rank(str(item["bucket"])) == _change_bucket_rank(str(existing["bucket"]))
            and _change_role_rank(str(item["change_role"])) < _change_role_rank(str(existing["change_role"]))
        ):
            existing["change_role"] = item["change_role"]
            existing["reason"] = item["reason"]
        if _confidence_rank(str(item["confidence"])) < _confidence_rank(str(existing["confidence"])):
            existing["confidence"] = item["confidence"]
        existing["_source_rank"] = min(int(existing.get("_source_rank", 99)), int(item.get("_source_rank", 99)))
        if not existing.get("symbol") and item.get("symbol"):
            existing["symbol"] = item["symbol"]
    return list(merged.values())


def _ordered_change_candidates(
    candidates: list[dict[str, object]],
    *,
    bucket: str,
    limit: int,
) -> list[dict[str, object]]:
    """Return one bounded ordered bucket of change-plan targets."""

    ordered = sorted(
        [item for item in candidates if str(item["bucket"]) == bucket],
        key=lambda item: (
            _change_role_rank(str(item["change_role"])),
            _confidence_rank(str(item["confidence"])),
            int(item.get("_source_rank", 99)),
            str(item["path"]),
            str(item.get("symbol") or ""),
        ),
    )
    return [
        {
            "path": str(item["path"]),
            "symbol": item.get("symbol"),
            "change_role": str(item["change_role"]),
            "confidence": str(item["confidence"]),
            "reason": str(item["reason"]),
        }
        for item in ordered[:limit]
    ]


def _derive_validation_impact(
    profile: dict[str, object],
    required_edits: list[dict[str, object]],
    likely_edits: list[dict[str, object]],
    optional_edits: list[dict[str, object]],
    *,
    limit: int,
) -> list[dict[str, object]]:
    """Return bounded validation surfaces likely affected by the change."""

    items: list[dict[str, object]] = []
    seen_paths: set[str] = set()
    anchor_label = str(profile.get("anchor_symbol") or profile.get("anchor_path") or "this change")
    for source in (required_edits, likely_edits, optional_edits):
        for item in source:
            path = str(item["path"])
            if path in seen_paths:
                continue
            if str(item.get("change_role")) == "validation":
                items.append(dict(item))
                seen_paths.add(path)
            elif str(item.get("change_role")) == "build_artifact":
                items.append(
                    {
                        "path": path,
                        "symbol": item.get("symbol"),
                        "change_role": "build_artifact",
                        "confidence": item.get("confidence", "medium"),
                        "reason": f"Rebuild or verify this generated artifact after changing {anchor_label} if your workflow commits built assets.",
                    }
                )
                seen_paths.add(path)
    if profile.get("service"):
        for item in required_edits + likely_edits:
            if str(item.get("change_role")) == "entrypoint" and str(item["path"]) not in seen_paths:
                items.append(
                    {
                        "path": str(item["path"]),
                        "symbol": item.get("symbol"),
                        "change_role": "validation",
                        "confidence": item.get("confidence", "high"),
                        "reason": f"Confirm the web-service registration still matches the updated API contract for {anchor_label}.",
                    }
                )
                break
    return items[:limit]


def _derive_recommended_sequence(
    profile: dict[str, object],
    required_edits: list[dict[str, object]],
    likely_edits: list[dict[str, object]],
    validation_impact: list[dict[str, object]],
    *,
    limit: int,
) -> list[dict[str, object]]:
    """Return a concise recommended inspection/update sequence."""

    steps: list[dict[str, object]] = []
    step_number = 1
    implementation = next((item for item in required_edits if item["change_role"] == "implementation"), None)
    if implementation is not None:
        steps.append(
            {
                "step": step_number,
                "action": "inspect_and_update",
                "target": implementation["path"],
                "why": implementation["reason"],
            }
        )
        step_number += 1
    entrypoint = next((item for item in required_edits + likely_edits if item["change_role"] == "entrypoint"), None)
    if entrypoint is not None:
        steps.append(
            {
                "step": step_number,
                "action": "update_entrypoint",
                "target": entrypoint["path"],
                "why": entrypoint["reason"],
            }
        )
        step_number += 1
    companion = next(
        (
            item
            for item in required_edits + likely_edits
            if item["change_role"] in {"rendering_companion", "form_companion", "framework_context"}
        ),
        None,
    )
    if companion is not None:
        steps.append(
            {
                "step": step_number,
                "action": "inspect_companion",
                "target": companion["path"],
                "why": companion["reason"],
            }
        )
        step_number += 1
    validation = next((item for item in validation_impact if item["change_role"] in {"validation", "build_artifact"}), None)
    if validation is not None:
        steps.append(
            {
                "step": step_number,
                "action": "validate_change",
                "target": validation["path"],
                "why": validation["reason"],
            }
        )
        step_number += 1
    optional_example = next((item for item in likely_edits if item["change_role"] == "example_reference"), None)
    if optional_example is not None:
        steps.append(
            {
                "step": step_number,
                "action": "review_reference_example",
                "target": optional_example["path"],
                "why": optional_example["reason"],
            }
        )
    return steps[:limit]


def _change_bucket_rank(bucket: str) -> int:
    """Return sortable rank for one plan bucket."""

    return {"required": 0, "likely": 1, "optional": 2}.get(bucket, 3)


def _change_role_rank(role: str) -> int:
    """Return sortable rank for one plan change role."""

    return {
        "implementation": 0,
        "entrypoint": 1,
        "validation": 2,
        "rendering_companion": 3,
        "form_companion": 4,
        "framework_context": 5,
        "build_artifact": 6,
        "example_reference": 7,
    }.get(role, 8)


def _file_role_for_plan_path(path: str) -> str:
    """Return a coarse file role for conservative planning heuristics."""

    if path.endswith("/db/services.php"):
        return "services_definition"
    if "/classes/external/" in path or path.endswith("/externallib.php"):
        return "external_api_class"
    if path.endswith("/renderer.php"):
        return "renderer_file"
    if path.endswith(".mustache"):
        return "template_file"
    if "/classes/output/" in path:
        return "output_class"
    if "/classes/form/" in path or path.endswith("_form.php"):
        return "form_class"
    if "/amd/src/" in path:
        return "amd_source"
    if "/amd/build/" in path:
        return "amd_build"
    if path.endswith("/locallib.php"):
        return "locallib_file"
    if path.endswith("/lib.php"):
        return "lib_file"
    if path.endswith("/settings.php"):
        return "settings_file"
    return "unknown"


def _none_if_empty(value: object) -> str | None:
    """Return ``None`` for empty values and a string otherwise."""

    if value in {None, ""}:
        return None
    return str(value)


def _merge_dependency_section_items(
    items: list[dict[str, object]],
    *,
    section_name: str,
    limit: int,
) -> list[dict[str, object]]:
    """Return one bounded dependency section with same-path items merged."""

    merged: dict[str, dict[str, object]] = {}
    for item in items:
        path_key = str(item["path"])
        existing = merged.get(path_key)
        if existing is None:
            candidate = dict(item)
            candidate["related_relationships"] = [str(item["relationship"])]
            merged[path_key] = candidate
            continue

        relationships = set(existing.get("related_relationships", []))
        relationships.add(str(item["relationship"]))
        existing["related_relationships"] = sorted(relationships)

        reasons = {part.strip() for part in str(existing["reason"]).split(" | ") if part.strip()}
        for part in str(item["reason"]).split(" | "):
            if part.strip():
                reasons.add(part.strip())
        existing["reason"] = " | ".join(sorted(reasons))

        if _confidence_rank(str(item["confidence"])) < _confidence_rank(str(existing["confidence"])):
            existing["confidence"] = item["confidence"]

        current_score = _dependency_section_priority(section_name, existing)
        candidate_score = _dependency_section_priority(section_name, item)
        if candidate_score < current_score:
            existing["type"] = item["type"]
            existing["relationship"] = item["relationship"]
            existing["symbol"] = item.get("symbol")
            if item.get("line") is not None:
                existing["line"] = item.get("line")

    ordered = sorted(
        merged.values(),
        key=lambda item: (
            -_dependency_item_score(section_name, item),
            _dependency_section_priority(section_name, item),
            str(item["path"]),
            str(item.get("symbol") or ""),
        ),
    )
    return ordered[:limit]


def _present_dependency_section_item(section_name: str, item: dict[str, object]) -> dict[str, object]:
    """Return the public Phase 4C item shape for one dependency item."""

    presented = {
        "path": item["path"],
        "symbol": item.get("symbol"),
        "type": item["type"],
        "relationship": item["relationship"],
        "confidence": item["confidence"],
        "score": _dependency_item_score(section_name, item),
        "explanation": _dependency_explanation(section_name, item),
        "related_relationships": item.get("related_relationships", []),
    }
    if item.get("line") is not None:
        presented["line"] = item["line"]
    suggested_actions = _dependency_suggested_actions(section_name, item)
    if suggested_actions:
        presented["suggested_actions"] = suggested_actions
    return presented


def _dependency_primary_focus(items: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return the top cross-section starting points for an agent."""

    if not items:
        return []

    eligible = [
        item
        for item in items
        if str(item["confidence"]) == "high"
        and float(item["score"]) >= 0.6
        and not _dependency_primary_focus_excluded(item)
    ]
    if any(str(item["relationship"]) == "service_definition" for item in eligible):
        eligible = [
            item
            for item in eligible
            if str(item["_section"]) not in {"linked_rendering_artifacts", "linked_framework"}
        ]
    if not eligible:
        return []

    ordered = sorted(
        eligible,
        key=lambda item: (
            -float(item["score"]),
            _dependency_primary_focus_priority(item),
            str(item["path"]),
            str(item.get("symbol") or ""),
        ),
    )
    focus: list[dict[str, object]] = []
    seen_paths: set[str] = set()
    for item in ordered:
        path = str(item["path"])
        if path in seen_paths:
            continue
        seen_paths.add(path)
        focus.append(
            {
                "path": path,
                "symbol": item.get("symbol"),
                "reason": item["explanation"],
                "confidence": item["confidence"],
                "score": item["score"],
            }
        )
        if len(focus) >= 4:
            break
    return focus[:4]


def _dependency_primary_focus_priority(item: dict[str, object]) -> int:
    """Prefer the most actionable starting points across sections."""

    section_name = str(item["_section"])
    relationship = str(item["relationship"])
    item_type = str(item["type"])
    symbol = str(item.get("symbol") or "")

    if relationship == "service_implementation":
        return 0
    if relationship == "service_definition":
        return 1
    if item_type == "service_test" or section_name == "linked_tests":
        return 2
    if item_type == "js_module" and relationship in {"js_superclass", "js_import"}:
        return 3
    if item_type == "form_class" and "\\" in symbol:
        return 4
    if item_type == "output_class":
        return 5
    if item_type == "renderer_file":
        return 6
    if item_type == "template_file":
        return 7
    if item_type == "form_class":
        return 8
    if item_type == "template_file":
        return 9
    if item_type == "js_build_artifact":
        return 10
    return 20


def _dependency_primary_focus_excluded(item: dict[str, object]) -> bool:
    """Return whether an item is too weak or indirect for primary focus."""

    section_name = str(item["_section"])
    item_type = str(item["type"])
    symbol = str(item.get("symbol") or "")
    if section_name == "linked_framework":
        return True
    if item_type in {"framework_base", "class_file"}:
        return True
    if item_type == "form_class" and "\\" not in symbol:
        return True
    return False


def _dependency_section_summary(section_name: str) -> str:
    """Return a short explanation of one dependency-neighborhood section."""

    return {
        "likely_callers": "Primary entrypoints or direct usages invoking this symbol or file.",
        "likely_callees": "Direct dependencies, implementations, or invoked modules this symbol or file relies on.",
        "linked_tests": "Tests validating behaviour of this symbol or feature slice.",
        "linked_services": "Web service registration and implementation companions attached to this slice.",
        "linked_rendering_artifacts": "Rendering companions such as output classes, renderers, and templates.",
        "linked_forms": "Concrete and intermediate form classes attached to this provider or workflow.",
        "linked_framework": "Shared framework or base classes that shape the current behavior.",
        "linked_javascript": "JavaScript companion modules directly connected to this slice.",
        "linked_build_artifacts": "Generated build artifacts that should be regenerated or verified after source changes.",
    }.get(section_name, "Bounded related artifacts for this local dependency neighborhood.")


def _dependency_item_score(section_name: str, item: dict[str, object]) -> float:
    """Return a normalized Phase 4C score in the range 0.0..1.0."""

    base_score = (
        _dependency_relationship_weight(section_name, item)
        + _dependency_confidence_weight(str(item["confidence"]))
        + _dependency_proximity_weight(str(item.get("anchor") or ""), str(item["path"]))
    )
    score = base_score
    score *= _dependency_confidence_multiplier(str(item["confidence"]))
    score *= _dependency_relationship_multiplier(section_name, item)
    score += _dependency_reinforcement_weight(item)
    return round(min(1.0, score), 2)


def _dependency_relationship_weight(section_name: str, item: dict[str, object]) -> float:
    """Return the base relationship weight for one dependency item."""

    relationship = str(item["relationship"])
    item_type = str(item["type"])

    if relationship in {
        "instance_method_call",
        "static_method_call",
        "function_call",
        "renderer_usage",
        "form_usage",
        "js_import",
        "js_imported_by",
        "js_superclass",
        "service_implementation",
        "form_class",
    }:
        return 0.4
    if relationship == "service_definition":
        return 0.4
    if relationship in {"service_test", "linked_test", "test_usage"} or item_type == "service_test":
        return 0.35
    if item_type in {"renderer_file", "template_file", "output_class", "js_build_artifact"}:
        return 0.25
    if item_type in {"framework_base", "class_file"} or section_name in {"linked_services", "linked_framework"}:
        return 0.15
    return 0.15


def _dependency_confidence_weight(confidence: str) -> float:
    """Return the configured confidence weight."""

    return {"high": 0.4, "medium": 0.25, "low": 0.0}.get(confidence, 0.0)


def _dependency_confidence_multiplier(confidence: str) -> float:
    """Return the Phase 4C.1 confidence multiplier."""

    return {"high": 1.0, "medium": 0.85, "low": 0.6}.get(confidence, 0.85)


def _dependency_proximity_weight(anchor: str, path: str) -> float:
    """Return the configured proximity weight."""

    if not anchor:
        return 0.1
    if anchor == path:
        return 0.3
    if _same_component_root(anchor, path):
        return 0.2
    return 0.1


def _dependency_reinforcement_weight(item: dict[str, object]) -> float:
    """Return the reinforcement bonus for multi-signal items."""

    related = item.get("related_relationships", [])
    additional = max(0, len(related) - 1)
    return 0.15 if additional > 0 else 0.0


def _dependency_relationship_multiplier(section_name: str, item: dict[str, object]) -> float:
    """Return the Phase 4C.1 relationship penalty multiplier."""

    relationship = str(item["relationship"])
    item_type = str(item["type"])
    if relationship in {"service_definition", "service_implementation", "service_test", "linked_test", "test_usage"}:
        return 1.0
    if relationship in {"instance_method_call", "static_method_call", "function_call", "renderer_usage", "form_usage", "js_import", "js_imported_by", "js_superclass"}:
        return 1.0
    if item_type in {"renderer_file", "template_file", "output_class", "js_build_artifact"}:
        return 0.8
    if item_type in {"framework_base", "class_file", "file"} or section_name in {"linked_services", "linked_framework"}:
        return 0.7
    return 1.0


def _dependency_explanation(section_name: str, item: dict[str, object]) -> str:
    """Return a decision-grade explanation for one dependency item."""

    relationship = str(item["relationship"])
    item_type = str(item["type"])
    path = str(item["path"])
    symbol = str(item.get("symbol") or "").strip()
    chain_role = str(item.get("chain_role") or "")

    if relationship == "service_definition":
        return "Registers the queried method as a web service entrypoint; update it if the API name, class mapping, or method signature changes."
    if relationship == "service_implementation":
        return "Implements the queried web service behavior; update it if the API logic, parameters, or return payload change."
    if relationship in {"service_test", "linked_test", "test_usage"} or item_type == "service_test":
        return "Concrete PHPUnit coverage for the queried behavior; update expected results if the implementation or API contract changes."
    if relationship == "renderer_usage":
        return "Direct renderer-side caller of the queried method; inspect it if the rendered output flow or returned data changes."
    if relationship == "form_usage":
        return "Direct form-side caller of the queried symbol; update it if submitted fields, validation, or downstream behavior change."
    if relationship == "instance_method_call":
        return "High-confidence direct caller of the queried method; update it if the method signature or returned behavior changes."
    if relationship == "static_method_call":
        return "Direct static caller of the queried method; update it if the API signature or class contract changes."
    if relationship == "function_call":
        return "Direct caller of the queried function; update it if the function signature or behavior changes."
    if relationship == "js_import":
        return f"Imports the queried JavaScript dependency{' (' + symbol + ')' if symbol else ''}; inspect it if the imported API or expected exports change."
    if relationship == "js_imported_by":
        return f"Imports the queried JavaScript module{' as ' + symbol if symbol else ''}; inspect it if source changes ripple into direct callers."
    if relationship == "js_superclass":
        return "Superclass module for the queried JavaScript code; inspect it if inherited client-side behavior changes."
    if relationship == "js_build_artifact":
        return "Built AMD artifact generated from the queried source module; regenerate or verify it if the source module changes."
    if item_type == "output_class":
        return "Output/renderable class in the queried feature flow; update it if the rendered data structure or exposed fields change."
    if item_type == "renderer_file":
        return "Renderer companion in the queried feature flow; update it if rendering logic or template selection changes."
    if item_type == "template_file":
        return "Mustache template used by the queried rendering flow; update it if the rendered structure or template fields change."
    if item_type == "form_class":
        if chain_role == "direct":
            return "Concrete form used by the queried provider or workflow; update it if form fields, defaults, or validation rules change."
        return "Intermediate form base in the queried workflow; inspect it if shared form fields or validation behavior change."
    if item_type == "framework_base" and path == "lib/formslib.php":
        return "Moodle form framework base behind the queried form chain; inspect it only if the change affects shared form framework behavior."
    if item_type == "framework_base":
        return "Framework companion shaping the queried workflow; inspect it if the change affects shared framework behavior."
    if item_type == "class_file":
        return "Base class implementation behind the queried symbol; inspect it if shared inherited behavior or parent contracts change."
    if section_name == "linked_services":
        return "Service companion in the queried feature slice; inspect it if the external API wiring changes."
    return str(item.get("reason") or "Supporting implementation surface for the queried dependency neighborhood; inspect it only if nearby direct links are insufficient.")


def _dependency_suggested_actions(section_name: str, item: dict[str, object]) -> list[str]:
    """Return optional high-confidence next-step hints for one item."""

    if str(item["confidence"]) != "high":
        return []

    relationship = str(item["relationship"])
    item_type = str(item["type"])
    path = str(item["path"])

    if relationship == "service_definition":
        return ["Update service registration if the method name, class mapping, or API parameters change."]
    if relationship == "service_implementation":
        return ["Update API logic if method behavior changes.", "Update return payload handling if the service contract changes."]
    if relationship in {"service_test", "linked_test", "test_usage"} or item_type == "service_test":
        return ["Update corresponding PHPUnit test expectations."]
    if item_type == "output_class":
        return ["Update renderable data assembly if output fields or structure change."]
    if item_type == "renderer_file":
        return ["Update renderer logic if output selection or rendering flow changes."]
    if item_type == "template_file":
        return ["Update Mustache template if output structure or exposed fields change."]
    if item_type == "form_class":
        return ["Update form fields or validation rules if this workflow changes."]
    if item_type == "framework_base" and path == "lib/formslib.php":
        return ["Inspect shared form framework behavior only if the change affects framework-level form handling."]
    if item_type == "class_file":
        return ["Check inherited behavior if the change affects shared parent logic."]
    if relationship == "js_import":
        return ["Update imported-module usage if the JavaScript API contract changes."]
    if relationship == "js_superclass":
        return ["Inspect inherited client-side behavior if subclass behavior changes."]
    if relationship == "js_build_artifact":
        return ["Regenerate or verify the built AMD artifact after source changes."]
    if relationship in {"instance_method_call", "static_method_call", "function_call", "renderer_usage", "form_usage"}:
        return ["Update this caller if the symbol signature or returned behavior changes."]
    return []


def _calibrate_dependency_section_scores(
    section_name: str,
    items: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Apply small Phase 4C.1 score calibration for clearer separation."""

    if len(items) >= 3:
        top_scores = [float(item["score"]) for item in items[:3]]
        if max(top_scores) - min(top_scores) <= 0.05:
            boost_index = min(
                range(3),
                key=lambda idx: (
                    _confidence_rank(str(items[idx]["confidence"])),
                    _dependency_primary_focus_priority({**items[idx], "_section": section_name}),
                    str(items[idx]["path"]),
                ),
            )
            items[boost_index]["score"] = round(min(1.0, float(items[boost_index]["score"]) + 0.05), 2)
    return sorted(
        items,
        key=lambda item: (
            -float(item["score"]),
            _dependency_section_priority(section_name, item),
            str(item["path"]),
            str(item.get("symbol") or ""),
        ),
    )


def _prune_dependency_section_items(
    section_name: str,
    items: list[dict[str, object]],
    *,
    limit: int,
) -> list[dict[str, object]]:
    """Remove clearly low-value items while keeping the section bounded."""

    pruned: list[dict[str, object]] = []
    for item in items:
        explanation = str(item.get("explanation") or "").strip()
        if not explanation or explanation.startswith("Supporting implementation surface"):
            continue
        if float(item["score"]) < 0.35 and str(item["confidence"]) == "low":
            continue
        if section_name == "linked_framework" and len(items) > 2:
            if str(item["type"]) == "class_file" and float(item["score"]) <= 0.5:
                continue
        pruned.append(item)
    return pruned[: min(limit, 8)]


def _dependency_section_priority(section_name: str, item: dict[str, object]) -> int:
    """Return a small ranking score for one dependency-neighborhood section item."""

    relationship = str(item["relationship"])
    item_type = str(item["type"])
    path = str(item["path"])

    if section_name == "likely_callers":
        if relationship == "service_definition":
            return 0
        if relationship in {"renderer_usage", "form_usage"}:
            return 5
        if relationship in {"static_method_call", "instance_method_call", "function_call"}:
            return 10
        if relationship in {"js_imported_by", "js_import_usage", "js_superclass_usage"}:
            return 15
        return 40

    if section_name == "likely_callees":
        if item_type == "service_implementation":
            return 0
        if item_type == "form_class":
            return 5
        if relationship == "js_superclass":
            return 8
        if relationship == "js_import":
            return 10
        return 40

    if section_name == "linked_tests":
        return 0 if _is_concrete_test_path(path) else 30

    if section_name == "linked_services":
        if relationship == "service_definition":
            return 0
        if item_type == "service_implementation":
            return 5
        return 20

    if section_name == "linked_rendering_artifacts":
        same_component = _same_component_root(str(item.get("anchor") or ""), path)
        has_next_hops = bool(item.get("has_next_hops"))
        if item_type == "output_class":
            return 0 if has_next_hops and same_component else 4 if same_component else 12
        if item_type == "renderer_file":
            return 2 if same_component else 8
        if item_type == "template_file":
            return 3 if same_component else 9
        return 20

    if section_name == "linked_forms":
        if item_type == "form_class":
            symbol = str(item.get("symbol") or "")
            if str(item.get("chain_role", "direct")) == "direct" and "\\" in symbol:
                return 0
            if str(item.get("chain_role", "direct")) == "direct":
                return 3
            return 5
        if item_type == "framework_base":
            return 10
        if item_type == "class_file":
            return 8
        return 20

    if section_name == "linked_framework":
        if path == "lib/formslib.php":
            return 0
        if item_type == "framework_base":
            return 5
        if item_type == "class_file":
            return 10
        return 20

    if section_name == "linked_javascript":
        if relationship == "js_superclass":
            return 0
        if relationship == "js_import":
            return 5
        if relationship == "js_imported_by":
            return 10
        return 20

    if section_name == "linked_build_artifacts":
        return 0

    return 50


def _artifact_navigation_items(
    linked_artifacts: dict[str, object],
    *,
    anchor: str | None,
    include_anchor_file: bool,
    include_entrypoints: bool = True,
    include_js_reverse: bool = True,
) -> list[dict[str, object]]:
    """Flatten existing linked artifacts into Phase 4A navigation items."""

    items: list[dict[str, object]] = []

    for service in linked_artifacts.get("services", []) or []:
        service_name = str(service["service_name"])
        source_file = service.get("source_file")
        if source_file:
            items.append(
                _navigation_item(
                    item_type="service_definition",
                    relationship="service_definition",
                    confidence="high",
                    reason=f"because this implementation is registered by service {service_name} in db/services.php",
                    path=str(source_file),
                    symbol=service_name,
                    anchor=anchor,
                )
            )
        for step in service.get("navigation_chain", []):
            items.append(
                _navigation_item(
                    item_type=_artifact_item_type(str(step["path"]), str(step.get("artifact_type"))),
                    relationship=str(step.get("artifact_type", "service_navigation")),
                    confidence=_artifact_confidence(str(step.get("artifact_type")), str(step.get("chain_role", "primary"))),
                    reason=str(step["reason"]),
                    path=str(step["path"]),
                    symbol=service_name if str(step.get("artifact_type")) == "service_definition" else None,
                    anchor=anchor,
                )
            )

    for rendering in linked_artifacts.get("rendering", []) or []:
        items.extend(_artifact_node_to_navigation_items(rendering, anchor=anchor))

    javascript = linked_artifacts.get("javascript")
    if isinstance(javascript, dict):
        for item in javascript.get("imports", []) or []:
            if item.get("file"):
                items.append(
                    _navigation_item(
                        item_type="js_module",
                        relationship="js_import",
                        confidence="high",
                        reason=f"because this JavaScript module imports {item['module_name']}",
                        path=str(item["file"]),
                        symbol=str(item["module_name"]),
                        anchor=anchor,
                    )
                )
        superclass = javascript.get("superclass")
        if isinstance(superclass, dict) and superclass.get("file"):
            items.append(
                _navigation_item(
                    item_type="js_module",
                    relationship="js_superclass",
                    confidence="high",
                    reason=str(superclass["reason"]),
                    path=str(superclass["file"]),
                    symbol=str(superclass.get("module_name") or ""),
                    anchor=anchor,
                )
            )
        build_artifact = javascript.get("build_artifact")
        if isinstance(build_artifact, dict):
            items.append(
                _navigation_item(
                    item_type="js_build_artifact",
                    relationship="js_build_artifact",
                    confidence="high",
                    reason=str(build_artifact["reason"]),
                    path=str(build_artifact["path"]),
                    symbol=None,
                    anchor=anchor,
                )
            )
        if include_js_reverse:
            for importer in javascript.get("imported_by", [])[:4]:
                items.append(
                    _navigation_item(
                        item_type="js_module",
                        relationship="js_imported_by",
                        confidence="medium",
                        reason=f"because this module is imported by {importer['module_name']}",
                        path=str(importer["file"]),
                        symbol=str(importer["module_name"]),
                        anchor=anchor,
                    )
                )

    if include_entrypoints:
        for entrypoint in linked_artifacts.get("entrypoints", []) or []:
            items.append(
                _navigation_item(
                    item_type=_artifact_item_type(str(entrypoint["path"]), str(entrypoint.get("artifact_type"))),
                    relationship=str(entrypoint.get("artifact_type") or "entrypoint_link"),
                    confidence=_artifact_confidence(str(entrypoint.get("artifact_type")), "supporting"),
                    reason=str(entrypoint["reason"]),
                    path=str(entrypoint["path"]),
                    symbol=None,
                    anchor=anchor,
                )
            )

    if include_anchor_file and anchor:
        items.append(
            _navigation_item(
                item_type="file",
                relationship="anchor_file",
                confidence="high",
                reason="because this file anchors the current feature slice",
                path=anchor,
                symbol=None,
                anchor=anchor,
            )
        )
    return items


def _artifact_node_to_navigation_items(
    node: dict[str, object],
    *,
    anchor: str | None,
) -> list[dict[str, object]]:
    """Return one artifact node and its bounded next hops as navigation items."""

    items = [
        _navigation_item(
            item_type=_artifact_item_type(str(node["path"]), str(node.get("artifact_type"))),
            relationship=str(node.get("artifact_type") or "linked_artifact"),
            confidence=_artifact_confidence(str(node.get("artifact_type")), str(node.get("chain_role", "direct"))),
            reason=str(node["reason"]),
            path=str(node["path"]),
            symbol=str(node.get("class_name") or "") or None,
            anchor=anchor,
        )
    ]
    for hop in node.get("next_hops", []) or []:
        items.extend(_artifact_node_to_navigation_items(hop, anchor=anchor))
    return items


def _navigation_item(
    *,
    item_type: str,
    relationship: str,
    confidence: str,
    reason: str,
    path: str,
    symbol: str | None,
    anchor: str | None,
) -> dict[str, object]:
    """Build a normalized Phase 4A navigation item."""

    return {
        "type": item_type,
        "relationship": relationship,
        "confidence": confidence,
        "reason": reason,
        "path": path,
        "symbol": symbol,
        "anchor": anchor,
    }


def _split_navigation_items(
    items: list[dict[str, object]],
    *,
    limit: int,
    primary_key: str = "primary_related_definitions",
    secondary_key: str = "secondary_related_definitions",
) -> dict[str, list[dict[str, object]]]:
    """Split navigation items into bounded primary and secondary groups."""

    merged: dict[tuple[str, str | None, str], dict[str, object]] = {}
    for item in items:
        key = (str(item["path"]), item.get("symbol"), str(item["relationship"]))
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(item)
            continue
        if str(item["reason"]) not in str(existing["reason"]):
            existing["reason"] = f"{existing['reason']} | {item['reason']}"
        if _confidence_rank(str(item["confidence"])) < _confidence_rank(str(existing["confidence"])):
            existing["confidence"] = item["confidence"]

    collapsed: dict[str, dict[str, object]] = {}
    for item in merged.values():
        path_key = str(item["path"])
        existing = collapsed.get(path_key)
        if existing is None:
            candidate = dict(item)
            candidate["related_relationships"] = [str(item["relationship"])]
            collapsed[path_key] = candidate
            continue

        existing_relationships = set(existing.get("related_relationships", []))
        existing_relationships.add(str(item["relationship"]))
        existing["related_relationships"] = sorted(existing_relationships)

        existing_reasons = {part.strip() for part in str(existing["reason"]).split(" | ") if part.strip()}
        for part in str(item["reason"]).split(" | "):
            if part.strip():
                existing_reasons.add(part.strip())
        existing["reason"] = " | ".join(sorted(existing_reasons))

        if _confidence_rank(str(item["confidence"])) < _confidence_rank(str(existing["confidence"])):
            existing["confidence"] = item["confidence"]

        current_score = (
            _confidence_rank(str(existing["confidence"])),
            _edit_surface_priority(str(existing["type"]), str(existing["relationship"]), str(existing["path"])),
            str(existing["path"]),
            str(existing.get("symbol") or ""),
        )
        candidate_score = (
            _confidence_rank(str(item["confidence"])),
            _edit_surface_priority(str(item["type"]), str(item["relationship"]), str(item["path"])),
            str(item["path"]),
            str(item.get("symbol") or ""),
        )
        if candidate_score < current_score:
            existing["type"] = item["type"]
            existing["relationship"] = item["relationship"]
            existing["symbol"] = item.get("symbol")
            existing["anchor"] = item.get("anchor")

    ordered = sorted(
        collapsed.values(),
        key=lambda item: (
            _confidence_rank(str(item["confidence"])),
            _edit_surface_priority(str(item["type"]), str(item["relationship"]), str(item["path"])),
            str(item["path"]),
            str(item.get("symbol") or ""),
        ),
    )
    primary = [item for item in ordered if str(item["confidence"]) == "high"][:limit]
    secondary_pool = [item for item in ordered if item not in primary]
    secondary = secondary_pool[:limit]
    return {
        primary_key: primary,
        secondary_key: secondary,
    }


def _artifact_item_type(path: str, artifact_type: str | None) -> str:
    """Return a stable Phase 4A item type."""

    if artifact_type:
        return artifact_type
    if path.endswith(".mustache"):
        return "template_file"
    if path.endswith("/renderer.php"):
        return "renderer_file"
    if "/amd/src/" in path:
        return "js_module"
    if "/amd/build/" in path:
        return "js_build_artifact"
    if "/classes/form/" in path or path.endswith("_form.php"):
        return "form_class"
    if path.endswith("_test.php") or path.endswith("_advanced_testcase.php"):
        return "service_test"
    return "file"


def _artifact_confidence(artifact_type: str | None, chain_role: str) -> str:
    """Return a small explicit confidence label for one artifact link."""

    high_types = {
        "service_definition",
        "service_implementation",
        "service_test",
        "output_class",
        "renderer_file",
        "template_file",
        "form_class",
        "framework_base",
        "js_import",
        "js_superclass",
        "js_build_artifact",
        "definition_file",
    }
    if artifact_type in high_types:
        return "high"
    if chain_role in {"primary", "direct"}:
        return "high"
    if chain_role in {"supporting", "derived", "verification"}:
        return "medium"
    return "medium"


def _suggestion_confidence(path: str, reason: str) -> str:
    """Return confidence for file-driven edit-surface fallback suggestions."""

    if path.endswith("/tests") or path.endswith("/version.php") or "/lang/en/" in path or path.endswith("/db/access.php"):
        return "medium"
    if "imports " in reason or "resolves to this file" in reason or "inherits from" in reason:
        return "high"
    return "medium"


def _confidence_rank(confidence: str) -> int:
    """Return sortable rank for a confidence label."""

    return {"high": 0, "medium": 1, "low": 2}.get(confidence, 3)


def _edit_surface_priority(item_type: str, relationship: str, path: str) -> int:
    """Return a lightweight priority for Phase 4A related/edit-surface items."""

    if relationship in {"definition_file", "anchor_file"}:
        return 0
    if relationship == "js_superclass":
        return 18
    if relationship == "js_import":
        return 19
    if relationship == "js_build_artifact":
        return 22
    if relationship == "js_imported_by":
        return 28
    if relationship == "service_definition":
        return 3
    if item_type in {"service_definition", "service_implementation", "service_test"}:
        return 5
    if item_type in {"output_class", "renderer_file", "template_file"}:
        return 10
    if item_type in {"form_class", "framework_base"}:
        return 15
    if item_type in {"js_module", "js_build_artifact"}:
        return 20
    if relationship in {"parent_definition", "overrides_definition", "implements_definition"}:
        return 25
    if relationship == "child_override":
        return 35
    if "/lang/en/" in path:
        return 80
    if path.endswith("/db/access.php"):
        return 85
    if path.endswith("/version.php"):
        return 90
    if path.endswith("/tests"):
        return 95
    return 50


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
            f.file_role,
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

    def add_artifact(
        class_name: str,
        relationship_type: str,
        target_file: str,
        *,
        next_hops: list[dict[str, object]] | None = None,
        chain_role: str = "direct",
        chain_depth: int = 0,
    ) -> None:
        file_exists = connection.execute(
            "SELECT 1 FROM files WHERE moodle_path = ? OR repository_relative_path = ?",
            (target_file, target_file),
        ).fetchone()
        artifacts.append(
            {
                "class_name": class_name,
                "relationship_type": relationship_type,
                "resolved_target_file": target_file,
                "resolved": bool(file_exists),
                "artifact_kind": _class_artifact_kind(target_file),
                "template_files": _existing_template_candidates(connection, target_file),
                "chain_role": chain_role,
                "chain_depth": chain_depth,
                "next_hops": next_hops or [],
            }
        )

    for item in relationships:
        if item["relationship_type"] not in {"references_class", "extends"}:
            continue
        class_name = str(item["target_name"]).lstrip("\\")
        relationship_key = (item["relationship_type"], class_name)
        if relationship_key in seen_relationships:
            continue
        seen_relationships.add(relationship_key)
        target_file = _resolve_class_artifact_target(connection, class_name)
        if target_file is None:
            continue
        next_hops: list[dict[str, object]] = []
        # Keep this bounded and explicit: only form classes currently grow a
        # small follow-on chain so provider -> form -> base -> framework flows
        # stay coherent without turning into open-ended graph traversal.
        if "/classes/form/" in target_file:
            next_hops = _class_chain_hops(connection, target_file, depth=3, level=1)
        add_artifact(class_name, str(item["relationship_type"]), target_file, next_hops=next_hops)
        if item["relationship_type"] == "extends" or (
            item["relationship_type"] == "references_class" and "/classes/form/" in target_file
        ):
            for ancestor_name, ancestor_file in _resolve_direct_parent_class_targets(connection, target_file):
                ancestor_key = ("extends_indirect", ancestor_name)
                if ancestor_key in seen_relationships:
                    continue
                seen_relationships.add(ancestor_key)
                add_artifact(ancestor_name, "extends_indirect", ancestor_file, chain_role="derived", chain_depth=1)
    return artifacts


def _class_chain_hops(
    connection: sqlite3.Connection,
    class_file: str,
    *,
    depth: int,
    level: int = 1,
    visited: set[str] | None = None,
) -> list[dict[str, object]]:
    """Return a bounded follow-on class chain for one resolved class file.

    This intentionally documents the workflow in code rather than inventing a
    general graph walk:
    - start from a directly referenced form class
    - follow a few explicit parent-class hops
    - stop once we hit the framework base or the depth cap
    """

    if depth <= 0:
        return []
    seen = set(visited or ())
    seen.add(class_file)
    hops: list[dict[str, object]] = []
    for ancestor_name, ancestor_file in _resolve_direct_parent_class_targets(connection, class_file):
        if ancestor_file in seen:
            continue
        short_ancestor = ancestor_name.split("\\")[-1]
        next_reason = (
            "suggested because this class inherits from moodleform through an indexed Moodle form/framework base"
            if ancestor_file == "lib/formslib.php"
            else f"suggested because this class inherits from {short_ancestor} through a resolved parent class chain"
        )
        hops.append(
            {
                "path": ancestor_file,
                "artifact_type": _class_artifact_kind(ancestor_file),
                "reason": next_reason,
                "indexed": True,
                "class_name": ancestor_name,
                "chain_role": "derived",
                "chain_depth": level,
                "next_hops": _class_chain_hops(
                    connection,
                    ancestor_file,
                    depth=depth - 1,
                    level=level + 1,
                    visited=seen | {ancestor_file},
                ),
            }
        )
    return hops


def _iter_artifact_nodes(items: list[dict[str, object]]) -> Iterator[dict[str, object]]:
    """Yield artifact nodes depth-first so follow-on hops can be flattened safely."""

    for item in items:
        yield item
        next_hops = item.get("next_hops")
        if isinstance(next_hops, list):
            yield from _iter_artifact_nodes([hop for hop in next_hops if isinstance(hop, dict)])


def _resolve_class_artifact_target(connection: sqlite3.Connection, class_name: str) -> str | None:
    """Resolve one class reference to a concrete file path when confidence is high."""

    target_file = resolve_classname_to_file_path(class_name)
    if target_file is not None:
        return target_file

    target_file = resolve_framework_class_to_file_path(class_name)
    if target_file is not None:
        return target_file

    symbol_rows = connection.execute(
        """
        SELECT files.moodle_path
        FROM symbols
        JOIN files ON files.id = symbols.file_id
        WHERE symbols.symbol_type IN ('class', 'interface', 'trait')
          AND symbols.name = ?
        ORDER BY files.moodle_path
        """,
        (class_name,),
    ).fetchall()
    if len(symbol_rows) == 1:
        return str(symbol_rows[0]["moodle_path"])
    return None


def _resolve_direct_parent_class_targets(
    connection: sqlite3.Connection,
    class_file: str,
) -> list[tuple[str, str]]:
    """Return one verified inheritance hop for a resolved class file."""

    rows = connection.execute(
        """
        SELECT target_name
        FROM relationships
        WHERE file_id = (
            SELECT id
            FROM files
            WHERE moodle_path = ? OR repository_relative_path = ?
            LIMIT 1
        )
          AND relationship_type = 'extends'
        ORDER BY line, target_name
        """,
        (class_file, class_file),
    ).fetchall()
    results: list[tuple[str, str]] = []
    for row in rows:
        class_name = str(row["target_name"]).lstrip("\\")
        target_file = _resolve_class_artifact_target(connection, class_name)
        if target_file is not None:
            results.append((class_name, target_file))
    return results


def _class_related_suggestions(class_references: list[dict[str, object]]) -> list[dict[str, str]]:
    """Return file-context related suggestions for resolved class artifacts."""

    suggestions: list[dict[str, str]] = []
    for item in _iter_artifact_nodes(class_references):
        if "resolved_target_file" in item:
            path = str(item["resolved_target_file"])
            reason = _class_artifact_reason(item)
            template_files = item.get("template_files", [])
        else:
            path = str(item["path"])
            reason = str(item["reason"])
            template_files = []
        suggestions.append(
            {
                "path": path,
                "reason": reason,
                "artifact_type": item.get("artifact_kind", item.get("artifact_type")),
                "chain_role": item.get("chain_role", "direct"),
                "chain_depth": item.get("chain_depth", 0),
            }
        )
        for template_path in template_files:
            suggestions.append(
                {
                    "path": template_path,
                    "reason": (
                        f"suggested because output class \\{item['class_name']} likely renders "
                        "through this Mustache template"
                    ),
                    "artifact_type": "template_file",
                    "chain_role": "supporting",
                    "chain_depth": item.get("chain_depth", 0) + 1,
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
    for item in _iter_artifact_nodes(artifacts):
        if "resolved_target_file" in item:
            target_path = str(item["resolved_target_file"])
            indexed = bool(item["resolved"])
            reason = _class_artifact_reason(item)
            template_files = item.get("template_files", [])
        else:
            target_path = str(item["path"])
            indexed = bool(item.get("indexed", False))
            reason = str(item["reason"])
            template_files = []
        if indexed:
            suggestions.append(
                {
                    "path": target_path,
                    "reason": reason,
                    "indexed": True,
                    "artifact_type": item.get("artifact_kind", item.get("artifact_type")),
                    "chain_role": item.get("chain_role", "direct"),
                    "chain_depth": item.get("chain_depth", 0),
                }
            )
        for template_path in template_files:
            suggestions.append(
                {
                    "path": template_path,
                    "reason": (
                        f"suggested because output class \\{item['class_name']} likely renders "
                        "through this Mustache template"
                    ),
                    "indexed": True,
                    "artifact_type": "template_file",
                    "chain_role": "supporting",
                    "chain_depth": item.get("chain_depth", 0) + 1,
                }
            )
    return suggestions


def _build_rendering_linked_artifacts(
    connection: sqlite3.Connection,
    file_row: sqlite3.Row,
    class_references: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return bounded rendering/navigation chains for one file.

    The workflow here stays intentionally small and explicit:
    - direct output/form references come first
    - one renderer/template companion layer is attached where confidence is high
    - form classes can already carry their own parent/base chain from
      ``_linked_class_artifacts``
    """

    moodle_path = str(file_row["moodle_path"])
    file_role = str(file_row["file_role"])
    component_root = _component_root_for_file(connection, moodle_path)
    artifacts: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()

    def add_artifact(
        path: str,
        artifact_type: str,
        reason: str,
        class_name: str | None = None,
        *,
        next_hops: list[dict[str, object]] | None = None,
        chain_role: str = "direct",
    ) -> None:
        key = (artifact_type, path)
        if key in seen:
            return
        seen.add(key)
        indexed = bool(
            connection.execute(
                "SELECT 1 FROM files WHERE moodle_path = ? OR repository_relative_path = ?",
                (path, path),
            ).fetchone()
        )
        artifacts.append(
            {
                "artifact_type": artifact_type,
                "path": path,
                "class_name": class_name,
                "indexed": indexed,
                "reason": reason,
                "chain_role": chain_role,
                "next_hops": next_hops or [],
            }
        )

    for item in class_references:
        target_path = str(item["resolved_target_file"])
        follow_on_hops: list[dict[str, object]] = [dict(hop) for hop in item.get("next_hops", [])]
        if item["artifact_kind"] == "output_class" and component_root:
            for template_path in item["template_files"]:
                follow_on_hops.append(
                    {
                        "path": template_path,
                        "artifact_type": "template_file",
                        "class_name": str(item["class_name"]),
                        "indexed": True,
                        "reason": (
                            f"suggested because output class \\{item['class_name']} likely renders "
                            "through this Mustache template"
                        ),
                        "chain_role": "supporting",
                        "next_hops": [],
                    }
                )
            for renderer_path in _renderer_candidates(connection, component_root):
                if target_path != renderer_path:
                    follow_on_hops.append(
                        {
                            "path": renderer_path,
                            "artifact_type": "renderer_file",
                            "indexed": True,
                            "reason": "suggested because this indexed renderer coordinates this component's output classes and templates",
                            "chain_role": "supporting",
                            "next_hops": [],
                        }
                    )
        add_artifact(
            target_path,
            str(item["artifact_kind"]),
            _class_artifact_reason(item),
            str(item["class_name"]),
            next_hops=follow_on_hops,
        )
        for template_path in item["template_files"]:
            add_artifact(
                template_path,
                "template_file",
                (
                    f"suggested because output class \\{item['class_name']} likely renders "
                    "through this Mustache template"
                ),
                str(item["class_name"]),
            )
        if component_root:
            for renderer_path in _renderer_candidates(connection, component_root):
                if target_path != renderer_path:
                    add_artifact(
                        renderer_path,
                        "renderer_file",
                        "suggested because this indexed renderer coordinates this component's output classes and templates",
                    )

    if file_role == "output_class" and component_root:
        template_path = _template_for_output_file(moodle_path)
        if template_path:
            add_artifact(
                template_path,
                "template_file",
                "suggested because this output class likely renders through this Mustache template",
            )
        for renderer_path in _renderer_candidates(connection, component_root):
            add_artifact(
                renderer_path,
                "renderer_file",
                "suggested because this indexed renderer commonly instantiates or renders this output class",
            )

    if file_role == "template_file" and component_root:
        output_path = _output_file_for_template(moodle_path)
        if output_path:
            add_artifact(
                output_path,
                "output_class",
                "suggested because this Mustache template likely pairs with this output class",
            )
        for renderer_path in _renderer_candidates(connection, component_root):
            add_artifact(
                renderer_path,
                "renderer_file",
                "suggested because this indexed renderer commonly renders this Mustache template",
            )

    if file_role == "renderer_file":
        output_rows = connection.execute(
            """
            SELECT moodle_path
            FROM files
            WHERE component_id = (
                SELECT component_id
                FROM files
                WHERE id = ?
            )
              AND file_role IN ('output_class', 'template_file')
            ORDER BY moodle_path
            LIMIT 6
            """,
            (file_row["id"],),
        ).fetchall()
        for item in output_rows:
            artifact_type = "template_file" if str(item["moodle_path"]).endswith(".mustache") else "output_class"
            add_artifact(
                str(item["moodle_path"]),
                artifact_type,
                "suggested because this renderer is closely coupled to these rendering artifacts",
            )

    return _sort_artifact_items(artifacts, limit=12)


def _template_for_output_file(output_file: str) -> str | None:
    """Return the paired Mustache template path for one output class file."""

    if "/classes/output/" not in output_file:
        return None
    component_root, suffix = output_file.split("/classes/output/", 1)
    return f"{component_root}/templates/{suffix.removesuffix('.php')}.mustache"


def _renderer_candidates(connection: sqlite3.Connection, component_root: str) -> list[str]:
    """Return verified renderer files for one component root."""

    output_renderer = f"{component_root}/classes/output/renderer.php"
    root_renderer = f"{component_root}/renderer.php"

    output_row = connection.execute(
        "SELECT moodle_path FROM files WHERE moodle_path = ? OR repository_relative_path = ? LIMIT 1",
        (output_renderer, output_renderer),
    ).fetchone()
    if output_row is not None:
        return [str(output_row["moodle_path"])]

    root_row = connection.execute(
        "SELECT moodle_path FROM files WHERE moodle_path = ? OR repository_relative_path = ? LIMIT 1",
        (root_renderer, root_renderer),
    ).fetchone()
    if root_row is not None:
        return [str(root_row["moodle_path"])]
    return []


def _output_file_for_template(template_file: str) -> str | None:
    """Return the paired output class path for one Mustache template."""

    if "/templates/" not in template_file or not template_file.endswith(".mustache"):
        return None
    component_root, suffix = template_file.split("/templates/", 1)
    return f"{component_root}/classes/output/{suffix.removesuffix('.mustache')}.php"


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


def _build_service_linked_artifacts(
    connection: sqlite3.Connection,
    webservices: list[sqlite3.Row],
) -> list[dict[str, object]]:
    """Return bounded service-definition navigation chains."""

    artifacts: list[dict[str, object]] = []
    for item in webservices:
        target_file = item["resolved_target_file"]
        linked_tests: list[dict[str, str]] = []
        if target_file:
            linked_tests = _service_tests_for_definition(connection, item)
        navigation_chain: list[dict[str, object]] = []
        if target_file:
            navigation_chain.append(
                {
                    "path": str(target_file),
                    "artifact_type": "service_implementation",
                    "indexed": True,
                    "reason": f"suggested because service {item['service_name']} resolves to this implementation file",
                    "chain_role": "primary",
                }
            )
        navigation_chain.extend(
            {
                "path": str(test["file"]),
                "artifact_type": "service_test",
                "indexed": True,
                "reason": str(test["reason"]),
                "chain_role": "verification",
            }
            for test in linked_tests
        )
        artifacts.append(
            {
                "service_name": item["service_name"],
                "resolution_type": item["resolution_type"],
                "resolution_status": item.get("resolution_status", "resolved"),
                "source_file": item.get("source_file"),
                "implementation_file": target_file,
                "classname": item["classname"],
                "classpath": item["classpath"],
                "methodname": item.get("methodname"),
                "related_tests": linked_tests,
                "chain_role": "primary",
                "navigation_chain": navigation_chain,
            }
        )
    return sorted(
        artifacts,
        key=lambda item: (
            0 if item["implementation_file"] else 1,
            0 if item["related_tests"] else 1,
            str(item["implementation_file"] or ""),
            str(item["service_name"]),
        ),
    )


def _service_artifacts_for_definition_file(
    connection: sqlite3.Connection,
    file_id: int,
    moodle_path: str,
) -> list[dict[str, object]]:
    """Return service navigation touching one definition file.

    This keeps ``find-definition`` coherent with file-level navigation by
    including both direct ``db/services.php`` rows and reverse links from a
    resolved implementation file back to its service definitions.
    """

    direct_rows = connection.execute(
        """
        SELECT
            webservices.service_name,
            webservices.line,
            webservices.classpath,
            webservices.classname,
            webservices.methodname,
            webservices.resolved_target_file,
            webservices.resolution_type,
            webservices.resolution_status,
            files.moodle_path AS source_file
        FROM webservices
        JOIN files ON files.id = webservices.file_id
        WHERE webservices.file_id = ?
        ORDER BY files.moodle_path, webservices.service_name, webservices.line
        """,
        (file_id,),
    ).fetchall()
    reverse_rows = connection.execute(
        """
        SELECT
            webservices.service_name,
            webservices.line,
            webservices.classpath,
            webservices.classname,
            webservices.methodname,
            webservices.resolved_target_file,
            webservices.resolution_type,
            webservices.resolution_status,
            files.moodle_path AS source_file
        FROM webservices
        JOIN files ON files.id = webservices.file_id
        WHERE webservices.resolved_target_file = ?
        ORDER BY files.moodle_path, webservices.service_name, webservices.line
        """,
        (moodle_path,),
    ).fetchall()

    merged: list[dict[str, object]] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for row_item in [*direct_rows, *reverse_rows]:
        key = (
            str(row_item["service_name"]),
            row_item["source_file"],
            row_item["resolved_target_file"],
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(dict(row_item))
    return _build_service_linked_artifacts(connection, merged)


def _service_tests_for_definition(
    connection: sqlite3.Connection,
    webservice: sqlite3.Row | dict[str, object],
) -> list[dict[str, str]]:
    """Return likely tests for one resolved service definition."""

    candidates = _service_test_candidates([webservice])
    linked: list[dict[str, str]] = []
    for item in candidates:
        exists = connection.execute(
            "SELECT 1 FROM files WHERE moodle_path = ? OR repository_relative_path = ?",
            (item["path"], item["path"]),
        ).fetchone()
        if exists:
            linked.append({"file": item["path"], "reason": item["reason"]})
    return linked


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


def _deduplicate_suggestions(
    suggestions: list[dict[str, str]],
    limit: int | None = None,
) -> list[dict[str, str]]:
    """Deduplicate non-index-aware suggestions by path, merging distinct reasons."""

    merged: dict[str, dict[str, str]] = {}
    for item in suggestions:
        existing = merged.get(item["path"])
        if existing is None:
            merged[item["path"]] = dict(item)
            continue
        if item["reason"] not in existing["reason"]:
            existing["reason"] = f"{existing['reason']} | {item['reason']}"
    ordered = sorted(
        merged.values(),
        key=lambda item: (
            _artifact_priority(
                str(item["path"]),
                str(item["reason"]),
                artifact_type=str(item.get("artifact_type")) if item.get("artifact_type") else None,
                chain_role=str(item.get("chain_role", "direct")),
            ),
            int(item.get("chain_depth", 0)),
            str(item["path"]),
        ),
    )
    return ordered[:limit] if limit is not None else ordered


def _deduplicate_indexed_suggestions(
    suggestions: list[dict[str, object]],
    limit: int | None = None,
) -> list[dict[str, object]]:
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
    ordered = sorted(
        merged.values(),
        key=lambda item: (
            _artifact_priority(
                str(item["path"]),
                str(item["reason"]),
                indexed=bool(item.get("indexed", False)),
                artifact_type=str(item.get("artifact_type")) if item.get("artifact_type") else None,
                chain_role=str(item.get("chain_role", "direct")),
            ),
            int(item.get("chain_depth", 0)),
            str(item["path"]),
        ),
    )
    return ordered[:limit] if limit is not None else ordered


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


def _prune_generic_suggestions(
    suggestions: list[dict[str, object]] | list[dict[str, str]],
) -> list[dict[str, object]] | list[dict[str, str]]:
    """Drop weak generic fallbacks when stronger concrete links already exist."""

    paths = [str(item["path"]) for item in suggestions]
    has_concrete_tests = any(_is_concrete_test_path(path) for path in paths)
    has_service_impl = any(_is_service_implementation_path(path) for path in paths)
    pruned: list[dict[str, object]] | list[dict[str, str]] = []
    for item in suggestions:
        path = str(item["path"])
        if has_concrete_tests and path.endswith("/tests"):
            continue
        if has_service_impl and path.endswith("/db/access.php"):
            continue
        pruned.append(item)
    return pruned


def _artifact_priority(
    path: str,
    reason: str,
    *,
    indexed: bool = True,
    artifact_type: str | None = None,
    chain_role: str = "direct",
) -> int:
    """Return a lightweight priority for navigation artifacts."""

    if path == "lib/adminlib.php":
        return 0
    if artifact_type == "framework_base" or path == "lib/formslib.php":
        return 0 if chain_role == "direct" else 19
    if _is_concrete_test_path(path):
        return 5
    if artifact_type == "service_implementation" or _is_service_implementation_path(path):
        return 10
    if "/classes/form/" in path and "resolved parent class chain" not in reason and "extends " not in reason:
        return 15
    if "/classes/form/" in path:
        return 18
    if artifact_type == "js_import" or artifact_type == "js_superclass" or "/amd/src/" in path:
        return 20
    if artifact_type == "output_class" or ("/classes/output/" in path and not path.endswith("/renderer.php")):
        return 25
    if artifact_type == "renderer_file" or path.endswith("/renderer.php"):
        return 30
    if artifact_type == "template_file" or ("/templates/" in path and path.endswith(".mustache")):
        return 35
    if artifact_type == "js_build_artifact" or "/amd/build/" in path:
        return 40
    if "/lang/en/" in path:
        return 80
    if path.endswith("/db/access.php"):
        return 85
    if path.endswith("/version.php"):
        return 90
    if path.endswith("/tests"):
        return 95
    if not indexed:
        return 98
    if "admin settings APIs" in reason:
        return 0
    return 50


def _is_concrete_test_path(path: str) -> bool:
    """Return whether a suggestion points at a concrete test file."""

    return path.endswith("_test.php") or path.endswith("_advanced_testcase.php")


def _is_service_implementation_path(path: str) -> bool:
    """Return whether a path looks like a concrete service implementation target."""

    return path.endswith("/externallib.php") or "/classes/external/" in path


def _sort_artifact_items(items: list[dict[str, object]], limit: int | None = None) -> list[dict[str, object]]:
    """Return artifact items ordered by signal quality."""

    ordered = sorted(
        items,
        key=lambda item: (
            _artifact_priority(
                str(item["path"]),
                str(item["reason"]),
                indexed=bool(item.get("indexed", False)),
                artifact_type=str(item.get("artifact_type")) if item.get("artifact_type") else None,
            ),
            str(item["path"]),
        ),
    )
    return ordered[:limit] if limit is not None else ordered


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
    if relationship_type == "extends_indirect" and artifact_kind == "framework_base":
        return (
            f"suggested because this class inherits from {class_name} through an indexed Moodle form/framework base"
        )
    if relationship_type == "extends_indirect":
        return f"suggested because this class inherits from {class_name} through a resolved parent class chain"
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


def _build_js_navigation_artifacts(
    connection: sqlite3.Connection,
    js_module: sqlite3.Row | None,
    js_imports: list[dict[str, object]],
) -> dict[str, object] | None:
    """Return a bounded JS navigation chain for one indexed module."""

    if js_module is None:
        return None
    imported_by_rows = connection.execute(
        """
        SELECT
            importer.module_name AS importer_module,
            f.moodle_path AS file,
            ji.line,
            ji.import_kind
        FROM js_imports ji
        JOIN js_modules importer ON importer.id = ji.js_module_id
        JOIN files f ON f.id = importer.file_id
        WHERE ji.module_name = ?
        ORDER BY f.moodle_path, ji.line, importer.module_name
        LIMIT 5
        """,
        (js_module["module_name"],),
    ).fetchall()
    superclass = None
    if js_module["superclass_module"]:
        superclass_resolution = resolve_js_module(connection, js_module["superclass_module"])
        if superclass_resolution.source_file:
            superclass = {
                "module_name": js_module["superclass_module"],
                "class_name": js_module["superclass_name"],
                "file": superclass_resolution.source_file,
                "build_file": superclass_resolution.build_file,
                "reason": (
                    f"suggested because this source module extends {js_module['superclass_name']} "
                    f"from {js_module['superclass_module']}"
                ),
            }
    build_artifact = None
    if js_module["build_file"]:
        build_artifact = {
            "path": js_module["build_file"],
            "reason": "built artifact generated from the AMD source module",
        }
    return {
        "module_name": js_module["module_name"],
        "source_file": js_module["moodle_path"],
        "build_artifact": build_artifact,
        "superclass": superclass,
        "imports": [
            {
                "module_name": item["module_name"],
                "file": item["resolved_target_file"],
                "build_file": item["build_file"],
                "import_kind": item["import_kind"],
                "imported_name": item["imported_name"],
                "local_name": item["local_name"],
                "resolution_status": item["resolution_status"],
                "resolution_strategy": item["resolution_strategy"],
            }
            for item in js_imports
        ],
        "imported_by": [
            {
                "module_name": item["importer_module"],
                "file": item["file"],
                "line": item["line"],
                "usage_kind": "js_import_usage",
                "reason": f"suggested because this module imports {js_module['module_name']}",
            }
            for item in imported_by_rows
        ],
    }


def _find_js_usage_examples(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    limit: int,
) -> list[dict[str, object]]:
    """Return bounded high-confidence usage examples for one JS module."""

    examples: list[tuple[int, dict[str, object]]] = []
    import_rows = connection.execute(
        """
        SELECT
            importer.module_name AS importer_module,
            f.moodle_path AS file,
            ji.line
        FROM js_imports ji
        JOIN js_modules importer ON importer.id = ji.js_module_id
        JOIN files f ON f.id = importer.file_id
        WHERE ji.module_name = ?
        ORDER BY f.moodle_path, ji.line, importer.module_name
        LIMIT 10
        """,
        (row["module_name"],),
    ).fetchall()
    for item in import_rows:
        usage_kind = "js_import_usage"
        if row["module_name"] == row["superclass_module"]:
            usage_kind = "js_superclass_usage"
        examples.append(
            (
                90,
                {
                    "file": item["file"],
                    "line": item["line"],
                    "usage_kind": usage_kind,
                    "confidence": "high",
                    "snippet": item["importer_module"],
                },
            )
        )
    superclass_rows = connection.execute(
        """
        SELECT
            jm.module_name,
            f.moodle_path,
            1 AS line
        FROM js_modules jm
        JOIN files f ON f.id = jm.file_id
        WHERE jm.superclass_module = ?
        ORDER BY f.moodle_path, jm.module_name
        LIMIT 10
        """,
        (row["module_name"],),
    ).fetchall()
    for item in superclass_rows:
        examples.append(
            (
                95,
                {
                    "file": item["moodle_path"],
                    "line": item["line"],
                    "usage_kind": "js_superclass_usage",
                    "confidence": "high",
                    "snippet": item["module_name"],
                },
            )
        )
    return _sorted_usage_examples(examples, limit)


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


def _component_root_for_file(connection: sqlite3.Connection, moodle_path: str) -> str | None:
    """Return the component root path for one indexed file path."""

    row = connection.execute(
        """
        SELECT c.root_path
        FROM files f
        JOIN components c ON c.id = f.component_id
        WHERE f.moodle_path = ? OR f.repository_relative_path = ?
        ORDER BY CASE WHEN f.moodle_path = ? THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (moodle_path, moodle_path, moodle_path),
    ).fetchone()
    return str(row["root_path"]) if row is not None else None


def _build_entrypoint_links(
    connection: sqlite3.Connection,
    file_row: sqlite3.Row,
    service_artifacts: list[dict[str, object]],
    rendering_artifacts: list[dict[str, object]],
    js_navigation: dict[str, object] | None,
) -> list[dict[str, object]]:
    """Return bounded, workflow-oriented entrypoint links for one file."""

    moodle_path = str(file_row["moodle_path"])
    file_role = str(file_row["file_role"])
    entrypoints: list[dict[str, object]] = []

    def add_link(path: str, artifact_type: str, reason: str) -> None:
        if any(item["path"] == path for item in entrypoints):
            return
        indexed = bool(
            connection.execute(
                "SELECT 1 FROM files WHERE moodle_path = ? OR repository_relative_path = ?",
                (path, path),
            ).fetchone()
        )
        entrypoints.append(
            {
                "path": path,
                "artifact_type": artifact_type,
                "indexed": indexed,
                "reason": reason,
            }
        )

    if file_role == "settings_file":
        add_link(
            "lib/adminlib.php",
            "framework_base",
            "suggested because settings.php uses Moodle admin settings APIs defined in lib/adminlib.php",
        )
    if file_role == "services_definition":
        for item in service_artifacts:
            if item["implementation_file"]:
                add_link(
                    str(item["implementation_file"]),
                    "service_implementation",
                    f"suggested because service {item['service_name']} resolves to this implementation file",
                )
            for test in item["related_tests"]:
                add_link(
                    str(test["file"]),
                    "service_test",
                    str(test["reason"]),
                )
    if file_role in {"lib_file", "locallib_file", "renderer_file", "output_class", "template_file"}:
        for item in rendering_artifacts[:6]:
            add_link(str(item["path"]), str(item["artifact_type"]), str(item["reason"]))
    if file_role == "amd_source" and js_navigation is not None:
        for item in js_navigation["imports"][:5]:
            if item["file"]:
                add_link(
                    str(item["file"]),
                    "js_import",
                    f"suggested because this source module imports {item['module_name']}",
                )
        if js_navigation.get("superclass") and js_navigation["superclass"]["file"]:
            add_link(
                str(js_navigation["superclass"]["file"]),
                "js_superclass",
                str(js_navigation["superclass"]["reason"]),
            )
        if js_navigation.get("build_artifact"):
            add_link(
                str(js_navigation["build_artifact"]["path"]),
                "js_build_artifact",
                str(js_navigation["build_artifact"]["reason"]),
            )

    if moodle_path.endswith("/db/services.php"):
        for item in service_artifacts:
            if item["implementation_file"]:
                add_link(
                    str(item["implementation_file"]),
                    "service_implementation",
                    f"suggested because db/services.php registers {item['service_name']} here",
                )
    return _sort_artifact_items(entrypoints, limit=10)


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

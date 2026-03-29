"""Query services over the SQLite index.

These functions implement the fixed Phase 1 CLI commands and keep SQL details
out of the command-line interface layer.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from moodle_indexer.components import resolve_classname_to_file_path
from moodle_indexer.errors import ValidationError
from moodle_indexer.paths import normalize_relative_lookup_path
from moodle_indexer.suggestions import suggest_related_files


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
    linked_tests = _linked_service_tests(connection, webservices)
    rendering_references = _linked_rendering_artifacts(connection, relationships)

    related_suggestions = [
        {"path": item.path, "reason": item.reason}
        for item in suggest_related_files(moodle_path)
    ]
    related_suggestions.extend(_service_related_suggestions(webservices))
    related_suggestions.extend(_service_test_suggestions(connection, webservices))
    related_suggestions.extend(_rendering_related_suggestions(rendering_references))
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
        WHERE file_id = ? AND relationship_type = 'references_class'
        ORDER BY line, target_name
        """,
        (row["id"],),
    ).fetchall()
    suggestions.extend(_indexed_rendering_suggestions(connection, rendering_relationships))
    suggestions = _deduplicate_indexed_suggestions(suggestions)
    return {
        "file": moodle_path,
        "repository_relative_path": row["repository_relative_path"],
        "moodle_path": moodle_path,
        "suggestions": suggestions,
    }


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


def _linked_rendering_artifacts(
    connection: sqlite3.Connection,
    relationships: list[sqlite3.Row],
) -> list[dict[str, object]]:
    """Return resolved output-class and template artifacts for one file."""

    artifacts: list[dict[str, object]] = []
    seen_classes: set[str] = set()
    for item in relationships:
        if item["relationship_type"] != "references_class":
            continue
        class_name = str(item["target_name"]).lstrip("\\")
        if class_name in seen_classes:
            continue
        seen_classes.add(class_name)
        output_file = resolve_classname_to_file_path(class_name)
        if output_file is None:
            continue
        file_exists = connection.execute(
            "SELECT 1 FROM files WHERE moodle_path = ? OR repository_relative_path = ?",
            (output_file, output_file),
        ).fetchone()
        template_files = _existing_template_candidates(connection, output_file)
        artifacts.append(
            {
                "class_name": class_name,
                "resolved_target_file": output_file,
                "resolved": bool(file_exists),
                "template_files": template_files,
            }
        )
    return artifacts


def _rendering_related_suggestions(rendering_references: list[dict[str, object]]) -> list[dict[str, str]]:
    """Return file-context related suggestions for resolved rendering artifacts."""

    suggestions: list[dict[str, str]] = []
    for item in rendering_references:
        suggestions.append(
            {
                "path": str(item["resolved_target_file"]),
                "reason": (
                    f"suggested because this file references class \\{item['class_name']}, "
                    "which resolves to this output file"
                ),
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


def _indexed_rendering_suggestions(
    connection: sqlite3.Connection,
    relationships: list[sqlite3.Row],
) -> list[dict[str, object]]:
    """Return suggest-related entries for output-class and template companions."""

    artifacts = _linked_rendering_artifacts(connection, relationships)
    suggestions: list[dict[str, object]] = []
    for item in artifacts:
        target_path = str(item["resolved_target_file"])
        if item["resolved"]:
            suggestions.append(
                {
                    "path": target_path,
                    "reason": (
                        f"suggested because this file references class \\{item['class_name']}, "
                        "which resolves to this output file"
                    ),
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
    """Deduplicate non-index-aware suggestions by path and reason."""

    seen: set[tuple[str, str]] = set()
    ordered: list[dict[str, str]] = []
    for item in suggestions:
        key = (item["path"], item["reason"])
        if key in seen:
            continue
        seen.add(key)
        ordered.append(item)
    return ordered


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

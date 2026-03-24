"""Query services over the SQLite index.

These functions implement the fixed Phase 1 CLI commands and keep SQL details
out of the command-line interface layer.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

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
            f.relative_path,
            f.file_role,
            c.name AS component_name
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        JOIN components c ON c.id = s.component_id
        WHERE s.name = ? OR s.fqname = ?
        ORDER BY s.fqname, f.relative_path, s.line
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
                "file": row["relative_path"],
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

    repository_root = _get_indexed_repository_root(connection)
    relative_path = _resolve_lookup_relative_path(connection, repository_root, file_path)

    row = connection.execute(
        """
        SELECT
            f.relative_path,
            f.absolute_path,
            f.file_role,
            f.extension,
            c.name AS component_name
        FROM files f
        JOIN components c ON c.id = f.component_id
        WHERE f.relative_path = ?
        """,
        (relative_path,),
    ).fetchone()
    if row is None:
        raise ValidationError(f"File not found in index: {relative_path}")

    symbols = connection.execute(
        """
        SELECT name, fqname, symbol_type, namespace, line
        FROM symbols
        JOIN files ON files.id = symbols.file_id
        WHERE files.relative_path = ?
        ORDER BY line, fqname
        """,
        (relative_path,),
    ).fetchall()
    capabilities = connection.execute(
        """
        SELECT name, line, captype, contextlevel, archetypes_json, riskbitmask
        FROM capabilities
        JOIN files ON files.id = capabilities.file_id
        WHERE files.relative_path = ?
        ORDER BY name
        """,
        (relative_path,),
    ).fetchall()
    strings = connection.execute(
        """
        SELECT string_key, string_value, line
        FROM language_strings
        JOIN files ON files.id = language_strings.file_id
        WHERE files.relative_path = ?
        ORDER BY string_key
        """,
        (relative_path,),
    ).fetchall()
    tests = connection.execute(
        """
        SELECT name, test_type, line
        FROM tests
        JOIN files ON files.id = tests.file_id
        WHERE files.relative_path = ?
        ORDER BY test_type, name
        """,
        (relative_path,),
    ).fetchall()
    capability_checks = connection.execute(
        """
        SELECT capability_name, function_name, line
        FROM capability_usages
        JOIN files ON files.id = capability_usages.file_id
        WHERE files.relative_path = ?
        ORDER BY line, capability_name
        """,
        (relative_path,),
    ).fetchall()
    string_usages = connection.execute(
        """
        SELECT string_key, component_name, line
        FROM language_string_usages
        JOIN files ON files.id = language_string_usages.file_id
        WHERE files.relative_path = ?
        ORDER BY line, string_key
        """,
        (relative_path,),
    ).fetchall()
    relationships = connection.execute(
        """
        SELECT source_fqname, target_name, relationship_type, line
        FROM relationships
        JOIN files ON files.id = relationships.file_id
        WHERE files.relative_path = ?
        ORDER BY line, relationship_type, source_fqname, target_name
        """,
        (relative_path,),
    ).fetchall()

    return {
        "file": row["relative_path"],
        "absolute_path": str((repository_root / row["relative_path"]).resolve()),
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
            }
            for item in capabilities
        ],
        "language_strings": [dict(item) for item in strings],
        "capability_checks": [dict(item) for item in capability_checks],
        "string_usages": [dict(item) for item in string_usages],
        "tests": [dict(item) for item in tests],
        "relationships": [dict(item) for item in relationships],
        "related_suggestions": [
            {"path": item.path, "reason": item.reason}
            for item in suggest_related_files(row["relative_path"])
        ],
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
        SELECT relative_path, file_role
        FROM files
        WHERE component_id = ?
        ORDER BY relative_path
        """,
        (component["id"],),
    ).fetchall()
    capabilities = connection.execute(
        """
        SELECT name, line, file_id
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
        "capabilities": [{"name": item["name"], "line": item["line"]} for item in capabilities],
        "language_strings": [{"string_key": item["string_key"], "line": item["line"]} for item in strings],
        "tests": [dict(item) for item in tests],
        "sample_symbols": [dict(item) for item in symbols],
    }


def suggest_related(connection: sqlite3.Connection, repository_root: Path, file_path: str) -> dict:
    """Return related-file suggestions for a repository file path."""

    relative_path = _resolve_lookup_relative_path(connection, repository_root, file_path)

    indexed = connection.execute(
        "SELECT 1 FROM files WHERE relative_path = ?",
        (relative_path,),
    ).fetchone()
    if indexed is None:
        raise ValidationError(f"File not found in index: {relative_path}")

    suggestions = []
    for suggestion in suggest_related_files(relative_path):
        exists = connection.execute(
            "SELECT 1 FROM files WHERE relative_path = ?",
            (suggestion.path,),
        ).fetchone()
        suggestions.append(
            {
                "path": suggestion.path,
                "reason": suggestion.reason,
                "indexed": bool(exists),
            }
        )
    return {"file": relative_path, "suggestions": suggestions}


def _resolve_lookup_relative_path(
    connection: sqlite3.Connection,
    repository_root: Path,
    file_path: str,
) -> str:
    """Resolve a CLI file argument into the repo-relative path stored in SQLite."""

    candidate = Path(file_path).expanduser()
    if not candidate.is_absolute():
        return normalize_relative_lookup_path(file_path)

    resolved = candidate.resolve()
    for root in [repository_root.resolve()]:
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError:
            continue

    raise ValidationError(f"File path is outside the indexed repository: {resolved}")


def _get_indexed_repository_root(connection: sqlite3.Connection) -> Path:
    """Return the repository root recorded in the SQLite index."""

    row = connection.execute(
        "SELECT root_path FROM repositories ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        raise ValidationError("Indexed repository metadata not found in database.")
    return Path(row["root_path"]).resolve()

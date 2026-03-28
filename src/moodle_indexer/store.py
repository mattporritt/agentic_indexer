"""SQLite persistence for the Phase 1 Moodle index.

The schema is intentionally normalized and modest in scope so that it remains
easy to evolve as later phases add deeper analysis.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA_STATEMENTS = [
    """
    PRAGMA foreign_keys = ON;
    """,
    """
    CREATE TABLE repositories (
        id INTEGER PRIMARY KEY,
        input_path TEXT NOT NULL,
        repository_root TEXT NOT NULL UNIQUE,
        application_root TEXT NOT NULL,
        layout_type TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE components (
        id INTEGER PRIMARY KEY,
        repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        component_type TEXT NOT NULL,
        root_path TEXT NOT NULL,
        UNIQUE(repository_id, name)
    );
    """,
    """
    CREATE TABLE files (
        id INTEGER PRIMARY KEY,
        repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
        component_id INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
        repository_relative_path TEXT NOT NULL,
        moodle_path TEXT NOT NULL,
        path_scope TEXT NOT NULL,
        absolute_path TEXT NOT NULL,
        file_role TEXT NOT NULL,
        extension TEXT NOT NULL,
        UNIQUE(repository_id, repository_relative_path)
    );
    """,
    """
    CREATE TABLE symbols (
        id INTEGER PRIMARY KEY,
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        component_id INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        fqname TEXT NOT NULL,
        symbol_type TEXT NOT NULL,
        namespace TEXT,
        container_name TEXT,
        line INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE relationships (
        id INTEGER PRIMARY KEY,
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        source_fqname TEXT NOT NULL,
        target_name TEXT NOT NULL,
        relationship_type TEXT NOT NULL,
        line INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE capabilities (
        id INTEGER PRIMARY KEY,
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        component_id INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        line INTEGER NOT NULL,
        captype TEXT,
        contextlevel TEXT,
        archetypes_json TEXT NOT NULL,
        riskbitmask TEXT,
        clonepermissionsfrom TEXT
    );
    """,
    """
    CREATE TABLE capability_usages (
        id INTEGER PRIMARY KEY,
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        component_id INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
        capability_name TEXT NOT NULL,
        function_name TEXT NOT NULL,
        line INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE language_strings (
        id INTEGER PRIMARY KEY,
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        component_id INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
        string_key TEXT NOT NULL,
        string_value TEXT NOT NULL,
        line INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE language_string_usages (
        id INTEGER PRIMARY KEY,
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        string_key TEXT NOT NULL,
        component_name TEXT,
        line INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE tests (
        id INTEGER PRIMARY KEY,
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        component_id INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        test_type TEXT NOT NULL,
        line INTEGER NOT NULL,
        related_symbol TEXT
    );
    """,
    """
    CREATE INDEX idx_files_component_id ON files(component_id);
    """,
    """
    CREATE INDEX idx_files_repository_relative_path ON files(repository_relative_path);
    """,
    """
    CREATE INDEX idx_files_moodle_path ON files(moodle_path);
    """,
    """
    CREATE INDEX idx_symbols_name ON symbols(name);
    """,
    """
    CREATE INDEX idx_symbols_fqname ON symbols(fqname);
    """,
    """
    CREATE INDEX idx_relationships_source ON relationships(source_fqname);
    """,
    """
    CREATE INDEX idx_capabilities_name ON capabilities(name);
    """,
    """
    CREATE INDEX idx_language_strings_key ON language_strings(string_key);
    """,
    """
    CREATE INDEX idx_tests_component_id ON tests(component_id);
    """,
]


def initialize_database(database_path: Path) -> sqlite3.Connection:
    """Create a fresh SQLite database for a full rebuild."""

    database_path.parent.mkdir(parents=True, exist_ok=True)
    if database_path.exists():
        database_path.unlink()
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.commit()
    return connection


def open_database(database_path: Path) -> sqlite3.Connection:
    """Open an existing database connection with row access."""

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    return connection


def insert_repository(
    connection: sqlite3.Connection,
    input_path: str,
    repository_root: str,
    application_root: str,
    layout_type: str,
) -> int:
    """Insert the repository row and return its id."""

    cursor = connection.execute(
        """
        INSERT INTO repositories (input_path, repository_root, application_root, layout_type)
        VALUES (?, ?, ?, ?)
        """,
        (input_path, repository_root, application_root, layout_type),
    )
    return int(cursor.lastrowid)


def insert_component(
    connection: sqlite3.Connection,
    repository_id: int,
    name: str,
    component_type: str,
    root_path: str,
) -> int:
    """Insert or look up a component id."""

    existing = connection.execute(
        "SELECT id FROM components WHERE repository_id = ? AND name = ?",
        (repository_id, name),
    ).fetchone()
    if existing:
        return int(existing["id"])
    cursor = connection.execute(
        """
        INSERT INTO components (repository_id, name, component_type, root_path)
        VALUES (?, ?, ?, ?)
        """,
        (repository_id, name, component_type, root_path),
    )
    return int(cursor.lastrowid)


def insert_file(
    connection: sqlite3.Connection,
    repository_id: int,
    component_id: int,
    repository_relative_path: str,
    moodle_path: str,
    path_scope: str,
    absolute_path: str,
    file_role: str,
    extension: str,
) -> int:
    """Insert a file row and return its id."""

    cursor = connection.execute(
        """
        INSERT INTO files (
            repository_id,
            component_id,
            repository_relative_path,
            moodle_path,
            path_scope,
            absolute_path,
            file_role,
            extension
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            repository_id,
            component_id,
            repository_relative_path,
            moodle_path,
            path_scope,
            absolute_path,
            file_role,
            extension,
        ),
    )
    return int(cursor.lastrowid)


def insert_symbol(connection: sqlite3.Connection, file_id: int, component_id: int, symbol: dict[str, Any]) -> None:
    """Insert one symbol row."""

    connection.execute(
        """
        INSERT INTO symbols (file_id, component_id, name, fqname, symbol_type, namespace, container_name, line)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_id,
            component_id,
            symbol["name"],
            symbol["fqname"],
            symbol["symbol_type"],
            symbol.get("namespace"),
            symbol.get("container_name"),
            symbol["line"],
        ),
    )


def insert_relationship(connection: sqlite3.Connection, file_id: int, relationship: dict[str, Any]) -> None:
    """Insert one relationship row."""

    connection.execute(
        """
        INSERT INTO relationships (file_id, source_fqname, target_name, relationship_type, line)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            file_id,
            relationship["source_fqname"],
            relationship["target_name"],
            relationship["relationship_type"],
            relationship["line"],
        ),
    )


def insert_capability(connection: sqlite3.Connection, file_id: int, component_id: int, capability: dict[str, Any]) -> None:
    """Insert one capability definition row."""

    connection.execute(
        """
        INSERT INTO capabilities (
            file_id,
            component_id,
            name,
            line,
            captype,
            contextlevel,
            archetypes_json,
            riskbitmask,
            clonepermissionsfrom
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_id,
            component_id,
            capability["name"],
            capability["line"],
            capability.get("captype"),
            capability.get("contextlevel"),
            json.dumps(capability.get("archetypes", {}), sort_keys=True),
            capability.get("riskbitmask"),
            capability.get("clonepermissionsfrom"),
        ),
    )


def insert_capability_usage(connection: sqlite3.Connection, file_id: int, component_id: int, usage: dict[str, Any]) -> None:
    """Insert one capability usage row."""

    connection.execute(
        """
        INSERT INTO capability_usages (file_id, component_id, capability_name, function_name, line)
        VALUES (?, ?, ?, ?, ?)
        """,
        (file_id, component_id, usage["capability_name"], usage["function_name"], usage["line"]),
    )


def insert_language_string(connection: sqlite3.Connection, file_id: int, component_id: int, language_string: dict[str, Any]) -> None:
    """Insert one language string definition row."""

    connection.execute(
        """
        INSERT INTO language_strings (file_id, component_id, string_key, string_value, line)
        VALUES (?, ?, ?, ?, ?)
        """,
        (file_id, component_id, language_string["string_key"], language_string["string_value"], language_string["line"]),
    )


def insert_language_string_usage(connection: sqlite3.Connection, file_id: int, usage: dict[str, Any]) -> None:
    """Insert one language string usage row."""

    connection.execute(
        """
        INSERT INTO language_string_usages (file_id, string_key, component_name, line)
        VALUES (?, ?, ?, ?)
        """,
        (file_id, usage["string_key"], usage.get("component_name"), usage["line"]),
    )


def insert_test(connection: sqlite3.Connection, file_id: int, component_id: int, test_record: dict[str, Any]) -> None:
    """Insert one test artifact row."""

    connection.execute(
        """
        INSERT INTO tests (file_id, component_id, name, test_type, line, related_symbol)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            file_id,
            component_id,
            test_record["name"],
            test_record["test_type"],
            test_record["line"],
            test_record.get("related_symbol"),
        ),
    )

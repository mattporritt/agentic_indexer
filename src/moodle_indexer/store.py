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
        signature TEXT,
        parameters_json TEXT NOT NULL,
        return_type TEXT,
        docblock_summary TEXT,
        docblock_tags_json TEXT NOT NULL,
        visibility TEXT,
        is_static INTEGER NOT NULL,
        is_final INTEGER NOT NULL,
        is_abstract INTEGER NOT NULL,
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
    CREATE TABLE webservices (
        id INTEGER PRIMARY KEY,
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        component_id INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
        service_name TEXT NOT NULL,
        line INTEGER NOT NULL,
        classpath TEXT,
        classname TEXT,
        methodname TEXT,
        resolved_target_file TEXT,
        resolution_type TEXT NOT NULL,
        resolution_status TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE js_modules (
        id INTEGER PRIMARY KEY,
        file_id INTEGER NOT NULL UNIQUE REFERENCES files(id) ON DELETE CASCADE,
        component_id INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
        module_name TEXT NOT NULL,
        export_kind TEXT,
        export_name TEXT,
        superclass_name TEXT,
        superclass_module TEXT,
        resolved_superclass_file TEXT,
        build_file TEXT,
        build_status TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE js_imports (
        id INTEGER PRIMARY KEY,
        js_module_id INTEGER NOT NULL REFERENCES js_modules(id) ON DELETE CASCADE,
        module_name TEXT NOT NULL,
        line INTEGER NOT NULL,
        import_kind TEXT NOT NULL,
        imported_name TEXT,
        local_name TEXT,
        resolved_target_file TEXT,
        resolution_status TEXT NOT NULL
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
    CREATE INDEX idx_webservices_component_id ON webservices(component_id);
    """,
    """
    CREATE INDEX idx_js_modules_component_id ON js_modules(component_id);
    """,
    """
    CREATE INDEX idx_js_modules_module_name ON js_modules(module_name);
    """,
    """
    CREATE INDEX idx_js_imports_module_name ON js_imports(module_name);
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
        INSERT INTO symbols (
            file_id,
            component_id,
            name,
            fqname,
            symbol_type,
            namespace,
            container_name,
            signature,
            parameters_json,
            return_type,
            docblock_summary,
            docblock_tags_json,
            visibility,
            is_static,
            is_final,
            is_abstract,
            line
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_id,
            component_id,
            symbol["name"],
            symbol["fqname"],
            symbol["symbol_type"],
            symbol.get("namespace"),
            symbol.get("container_name"),
            symbol.get("signature"),
            json.dumps(symbol.get("parameters", []), sort_keys=True),
            symbol.get("return_type"),
            symbol.get("docblock_summary"),
            json.dumps(symbol.get("docblock_tags", {}), sort_keys=True),
            symbol.get("visibility"),
            int(bool(symbol.get("is_static"))),
            int(bool(symbol.get("is_final"))),
            int(bool(symbol.get("is_abstract"))),
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


def insert_webservice(
    connection: sqlite3.Connection,
    file_id: int,
    component_id: int,
    webservice: dict[str, Any],
) -> None:
    """Insert one service definition row."""

    connection.execute(
        """
        INSERT INTO webservices (
            file_id,
            component_id,
            service_name,
            line,
            classpath,
            classname,
            methodname,
            resolved_target_file,
            resolution_type,
            resolution_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_id,
            component_id,
            webservice["service_name"],
            webservice["line"],
            webservice.get("classpath"),
            webservice.get("classname"),
            webservice.get("methodname"),
            webservice.get("resolved_target_file"),
            webservice["resolution_type"],
            webservice["resolution_status"],
        ),
    )


def insert_js_module(
    connection: sqlite3.Connection,
    file_id: int,
    component_id: int,
    js_module: dict[str, Any],
) -> int:
    """Insert one indexed JS module row and return its id."""

    cursor = connection.execute(
        """
        INSERT INTO js_modules (
            file_id,
            component_id,
            module_name,
            export_kind,
            export_name,
            superclass_name,
            superclass_module,
            resolved_superclass_file,
            build_file,
            build_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_id,
            component_id,
            js_module["module_name"],
            js_module.get("export_kind"),
            js_module.get("export_name"),
            js_module.get("superclass_name"),
            js_module.get("superclass_module"),
            js_module.get("resolved_superclass_file"),
            js_module.get("build_file"),
            js_module["build_status"],
        ),
    )
    return int(cursor.lastrowid)


def insert_js_import(connection: sqlite3.Connection, js_module_id: int, js_import: dict[str, Any]) -> None:
    """Insert one JS import/dependency row."""

    connection.execute(
        """
        INSERT INTO js_imports (
            js_module_id,
            module_name,
            line,
            import_kind,
            imported_name,
            local_name,
            resolved_target_file,
            resolution_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            js_module_id,
            js_import["module_name"],
            js_import["line"],
            js_import["import_kind"],
            js_import.get("imported_name"),
            js_import.get("local_name"),
            js_import.get("resolved_target_file"),
            js_import["resolution_status"],
        ),
    )

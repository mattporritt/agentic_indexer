"""Moodle JavaScript module resolution helpers.

This module defines the current deterministic resolution model for Moodle AMD source
modules. Query-time resolution prefers the indexed JS module registry and falls
back to deterministic Moodle path rules only when the registry has no exact
match.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from moodle_indexer.components import component_root_from_name, resolve_amd_build_path


EXTERNAL_JS_MODULES = {
    "jquery",
    "jqueryui",
    "underscore",
}


@dataclass(slots=True)
class JsModuleResolution:
    """One resolved or unresolved Moodle JS module reference."""

    module_name: str
    source_file: str | None
    build_file: str | None
    resolution_status: str
    resolution_strategy: str
    component_name: str | None = None
    is_external: bool = False


def is_external_js_module(module_name: str) -> bool:
    """Return whether a JS module specifier is an external runtime dependency."""

    normalized = module_name.strip().strip("'\"")
    return normalized in EXTERNAL_JS_MODULES


def resolve_js_module_via_fallback(
    module_name: str,
    component_root: str | None = None,
) -> JsModuleResolution:
    """Resolve a Moodle JS module with deterministic non-registry rules."""

    normalized = module_name.strip().strip("'\"")
    if is_external_js_module(normalized):
        return JsModuleResolution(
            module_name=normalized,
            source_file=None,
            build_file=None,
            resolution_status="external",
            resolution_strategy="external_runtime",
            is_external=True,
        )

    if "/" not in normalized:
        return JsModuleResolution(
            module_name=normalized,
            source_file=None,
            build_file=None,
            resolution_status="unresolved",
            resolution_strategy="unresolved",
        )

    component_name, module_suffix = normalized.split("/", 1)
    root_path = component_root or component_root_from_name(component_name)
    if root_path is None or not module_suffix:
        return JsModuleResolution(
            module_name=normalized,
            source_file=None,
            build_file=None,
            resolution_status="unresolved",
            resolution_strategy="unresolved",
            component_name=component_name,
        )

    source_file = f"{root_path}/amd/src/{module_suffix}.js"
    return JsModuleResolution(
        module_name=normalized,
        source_file=source_file,
        build_file=resolve_amd_build_path(source_file),
        resolution_status="resolved",
        resolution_strategy="component_root_fallback",
        component_name=component_name,
    )


def resolve_js_module(
    connection: sqlite3.Connection | None,
    module_name: str,
) -> JsModuleResolution:
    """Resolve a Moodle JS module specifier.

    Resolution precedence:
    1. Exact hit in the indexed JS module registry
    2. Explicit external runtime dependency classification
    3. Indexed component-root lookup with deterministic Moodle path mapping
    4. Static component-root fallback rules
    5. Explicit unresolved result
    """

    normalized = module_name.strip().strip("'\"")
    if connection is not None:
        registry_hit = connection.execute(
            """
            SELECT
                jm.module_name,
                f.moodle_path AS source_file,
                jm.build_file,
                c.name AS component_name
            FROM js_modules jm
            JOIN files f ON f.id = jm.file_id
            JOIN components c ON c.id = jm.component_id
            WHERE jm.module_name = ?
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()
        if registry_hit is not None:
            return JsModuleResolution(
                module_name=registry_hit["module_name"],
                source_file=registry_hit["source_file"],
                build_file=registry_hit["build_file"],
                resolution_status="resolved",
                resolution_strategy="indexed_registry",
                component_name=registry_hit["component_name"],
            )

        if is_external_js_module(normalized):
            return resolve_js_module_via_fallback(normalized)

        component_root = _component_root_from_index(connection, normalized)
        if component_root is not None:
            return resolve_js_module_via_fallback(normalized, component_root=component_root)

    return resolve_js_module_via_fallback(normalized)


def _component_root_from_index(connection: sqlite3.Connection, module_name: str) -> str | None:
    """Return an indexed component root path for a JS module specifier."""

    if "/" not in module_name:
        return None
    component_name, _ = module_name.split("/", 1)
    row = connection.execute(
        "SELECT root_path FROM components WHERE name = ? ORDER BY id LIMIT 1",
        (component_name,),
    ).fetchone()
    if row is None:
        return None
    return str(row["root_path"])

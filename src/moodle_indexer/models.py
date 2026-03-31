"""Shared dataclasses for extracted Moodle entities.

These lightweight models let the scanning, extraction, persistence, and query
layers exchange structured data without tightly coupling to SQLite row shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class RepositoryRecord:
    """A repository being indexed."""

    input_path: str
    repository_root: Path
    application_root: Path
    layout_type: str


@dataclass(slots=True)
class ComponentRecord:
    """Represents a Moodle component inferred from repository paths."""

    name: str
    component_type: str
    root_path: str


@dataclass(slots=True)
class FileRecord:
    """Represents one indexed file and its Moodle-specific metadata."""

    repository_relative_path: str
    moodle_path: str
    path_scope: str
    absolute_path: str
    component_name: str
    file_role: str
    extension: str


@dataclass(slots=True)
class SymbolRecord:
    """Represents a PHP symbol definition."""

    name: str
    fqname: str
    symbol_type: str
    file_path: str
    component_name: str
    line: int
    namespace: str | None = None
    container_name: str | None = None


@dataclass(slots=True)
class RelationshipRecord:
    """Represents a simple symbol-to-symbol relationship."""

    source_fqname: str
    target_name: str
    relationship_type: str
    file_path: str
    line: int


@dataclass(slots=True)
class CapabilityRecord:
    """Represents a capability defined in ``db/access.php``."""

    name: str
    component_name: str
    file_path: str
    line: int
    captype: str | None = None
    contextlevel: str | None = None
    archetypes: dict[str, str] = field(default_factory=dict)
    riskbitmask: str | None = None
    clonepermissionsfrom: str | None = None


@dataclass(slots=True)
class CapabilityUsageRecord:
    """Represents an obvious capability check call found in PHP code."""

    capability_name: str
    function_name: str
    file_path: str
    line: int
    component_name: str


@dataclass(slots=True)
class LanguageStringRecord:
    """Represents one language string defined in ``lang/en/*.php``."""

    string_key: str
    string_value: str
    component_name: str
    file_path: str
    line: int


@dataclass(slots=True)
class LanguageStringUsageRecord:
    """Represents an obvious ``get_string`` usage."""

    string_key: str
    component_name: str | None
    file_path: str
    line: int


@dataclass(slots=True)
class WebServiceRecord:
    """Represents one service function defined in ``db/services.php``."""

    service_name: str
    component_name: str
    file_path: str
    line: int
    classpath: str | None = None
    classname: str | None = None
    methodname: str | None = None
    resolved_target_file: str | None = None
    resolution_type: str = "unresolved"
    resolution_status: str = "unresolved"


@dataclass(slots=True)
class JsModuleRecord:
    """Represents one Moodle AMD source module."""

    module_name: str
    component_name: str
    file_path: str
    export_kind: str | None = None
    export_name: str | None = None
    superclass_name: str | None = None
    superclass_module: str | None = None
    resolved_superclass_file: str | None = None
    build_file: str | None = None
    build_status: str = "unresolved"


@dataclass(slots=True)
class JsImportRecord:
    """Represents one import or AMD dependency for a Moodle JS source file."""

    module_name: str
    file_path: str
    component_name: str
    line: int
    import_kind: str
    imported_name: str | None = None
    local_name: str | None = None
    resolved_target_file: str | None = None
    resolution_status: str = "unresolved"


@dataclass(slots=True)
class TestRecord:
    """Represents a discovered test artifact."""

    name: str
    test_type: str
    file_path: str
    component_name: str
    line: int
    related_symbol: str | None = None

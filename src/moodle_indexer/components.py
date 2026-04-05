"""Moodle component inference from repository paths.

Moodle mixes top-level plugin families, nested plugin families, and core
subsystems. This module centralizes those path rules so the rest of the system
can reason in terms of stable component identifiers instead of ad hoc path
inspection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from moodle_indexer.subplugins import SubpluginMount


CORE_COMPONENT_ROOTS = {
    "core": "lib",
    "core_admin": "admin",
    "core_course": "course",
    "core_user": "user",
    "core_group": "group",
    "core_calendar": "calendar",
    "core_files": "files",
    "core_tag": "tag",
    "core_backup": "backup",
    "core_badges": "badges",
    "core_grade": "grade",
    "core_message": "message",
    "core_question": "question",
    "core_availability": "availability",
    "core_contentbank": "contentbank",
    "core_payment": "payment",
    "core_media": "media",
    "core_ai": "ai",
}


@dataclass(slots=True)
class InferredComponent:
    """The inferred Moodle component for a repository path."""

    name: str
    component_type: str
    root_path: str


def _plugin_component(
    component_name: str,
    component_type: str,
    root_parts: list[str],
) -> InferredComponent:
    """Build a plugin component record from its Moodle path parts."""

    return InferredComponent(component_name, component_type, "/".join(root_parts))


def component_root_from_name(component_name: str) -> str | None:
    """Return the Moodle root path for a known frankenstyle component name."""

    if component_name in CORE_COMPONENT_ROOTS:
        return CORE_COMPONENT_ROOTS[component_name]

    prefix_mappings = {
        "tool_": "admin/tool",
        "report_": "admin/report",
        "aiprovider_": "ai/provider",
        "format_": "course/format",
        "qtype_": "question/type",
        "qbehaviour_": "question/behaviour",
        "qformat_": "question/format",
        "availability_": "availability/condition",
        "gradereport_": "grade/report",
        "gradeexport_": "grade/export",
        "gradeimport_": "grade/import",
        "media_": "media/player",
        "paygw_": "payment/gateway",
        "contenttype_": "contentbank/contenttype",
        "message_": "message/output",
        "blocks_": "blocks",
        "mod_": "mod",
        "local_": "local",
        "theme_": "theme",
        "auth_": "auth",
        "enrol_": "enrol",
        "repository_": "repository",
        "filter_": "filter",
        "editor_": "editor",
        "portfolio_": "portfolio",
        "plagiarism_": "plagiarism",
    }
    for prefix, root in prefix_mappings.items():
        if component_name.startswith(prefix):
            suffix = component_name.removeprefix(prefix)
            return f"{root}/{suffix}"
    return None


def infer_js_module_name(relative_path: str, component_name: str) -> str | None:
    """Infer the Moodle AMD module name for a source file under ``amd/src``."""

    if "/amd/src/" not in relative_path or not relative_path.endswith(".js"):
        return None
    module_suffix = relative_path.split("/amd/src/", 1)[1].removesuffix(".js")
    if not module_suffix:
        return None
    return f"{component_name}/{module_suffix}"


def resolve_js_module_to_source_path(module_name: str) -> str | None:
    """Resolve a Moodle AMD module name to its canonical ``amd/src`` file."""

    normalized = module_name.strip().strip("'\"")
    if "/" not in normalized:
        return None
    component_name, module_suffix = normalized.split("/", 1)
    component_root = component_root_from_name(component_name)
    if component_root is None or not module_suffix:
        return None
    return f"{component_root}/amd/src/{module_suffix}.js"


def resolve_amd_build_path(source_path: str) -> str | None:
    """Resolve an ``amd/src`` source path to its built ``amd/build`` artifact."""

    if "/amd/src/" not in source_path or not source_path.endswith(".js"):
        return None
    return source_path.replace("/amd/src/", "/amd/build/").removesuffix(".js") + ".min.js"


def resolve_classname_to_file_path(classname: str) -> str | None:
    """Resolve a Moodle class name to its expected autoloaded PHP file path."""

    normalized = classname.lstrip("\\")
    namespace_parts = normalized.split("\\")
    if not namespace_parts:
        return None
    component_root = component_root_from_name(namespace_parts[0])
    if component_root is None or len(namespace_parts) < 2:
        return None
    relative_parts = [part for part in namespace_parts[1:] if part]
    if not relative_parts:
        return None
    return f"{component_root}/classes/{'/'.join(relative_parts)}.php"


def resolve_framework_class_to_file_path(classname: str) -> str | None:
    """Resolve well-known legacy Moodle framework classes to core files.

    Moodle still uses a few important non-namespaced framework classes. The
    index only needs a small explicit mapping so related-file suggestions can
    surface the core implementation files developers commonly need beside
    plugin code.
    """

    normalized = classname.lstrip("\\").lower()
    framework_mappings = {
        "moodleform": "lib/formslib.php",
    }
    return framework_mappings.get(normalized)


def infer_component(relative_path: str, subplugin_mounts: Sequence[SubpluginMount] | None = None) -> InferredComponent:
    """Infer the Moodle component from a repository-relative path.

    The rules favour explicit Moodle conventions first and only fall back to
    coarse core subsystem mapping when a path does not belong to a known plugin
    family.
    """

    parts = [part for part in relative_path.split("/") if part]
    if not parts:
        return InferredComponent("core", "core", ".")

    if subplugin_mounts:
        subplugin_component = _infer_subplugin_component(parts, subplugin_mounts)
        if subplugin_component is not None:
            return subplugin_component

    if len(parts) >= 3 and parts[0] == "admin" and parts[1] == "tool":
        return _plugin_component(f"tool_{parts[2]}", "tool", parts[:3])
    if len(parts) >= 3 and parts[0] == "admin" and parts[1] == "report":
        return _plugin_component(f"report_{parts[2]}", "report", parts[:3])
    if len(parts) >= 3 and parts[0] == "course" and parts[1] == "format":
        return _plugin_component(f"format_{parts[2]}", "format", parts[:3])
    if len(parts) >= 3 and parts[0] == "question" and parts[1] == "type":
        return _plugin_component(f"qtype_{parts[2]}", "qtype", parts[:3])
    if len(parts) >= 3 and parts[0] == "question" and parts[1] == "behaviour":
        return _plugin_component(f"qbehaviour_{parts[2]}", "qbehaviour", parts[:3])
    if len(parts) >= 3 and parts[0] == "question" and parts[1] == "format":
        return _plugin_component(f"qformat_{parts[2]}", "qformat", parts[:3])
    if len(parts) >= 3 and parts[0] == "availability" and parts[1] == "condition":
        return _plugin_component(f"availability_{parts[2]}", "availability", parts[:3])
    if len(parts) >= 3 and parts[0] == "grade" and parts[1] == "report":
        return _plugin_component(f"gradereport_{parts[2]}", "gradereport", parts[:3])
    if len(parts) >= 3 and parts[0] == "grade" and parts[1] == "export":
        return _plugin_component(f"gradeexport_{parts[2]}", "gradeexport", parts[:3])
    if len(parts) >= 3 and parts[0] == "grade" and parts[1] == "import":
        return _plugin_component(f"gradeimport_{parts[2]}", "gradeimport", parts[:3])
    if len(parts) >= 3 and parts[0] == "media" and parts[1] == "player":
        return _plugin_component(f"media_{parts[2]}", "media", parts[:3])
    if len(parts) >= 3 and parts[0] == "payment" and parts[1] == "gateway":
        return _plugin_component(f"paygw_{parts[2]}", "paygw", parts[:3])
    if len(parts) >= 3 and parts[0] == "contentbank" and parts[1] == "contenttype":
        return _plugin_component(f"contenttype_{parts[2]}", "contenttype", parts[:3])
    if len(parts) >= 3 and parts[0] == "message" and parts[1] == "output":
        return _plugin_component(f"message_{parts[2]}", "message", parts[:3])
    if len(parts) >= 3 and parts[0] == "ai" and parts[1] == "provider":
        return _plugin_component(f"aiprovider_{parts[2]}", "aiprovider", parts[:3])

    top_level_families = {
        "mod",
        "blocks",
        "local",
        "theme",
        "auth",
        "enrol",
        "repository",
        "filter",
        "editor",
        "portfolio",
        "plagiarism",
        "report",
    }
    if len(parts) >= 2 and parts[0] in top_level_families:
        return _plugin_component(f"{parts[0]}_{parts[1]}", parts[0], parts[:2])

    core_prefixes = {
        "lib": "core",
        "admin": "core_admin",
        "ai": "core_ai",
        "course": "core_course",
        "user": "core_user",
        "group": "core_group",
        "calendar": "core_calendar",
        "files": "core_files",
        "tag": "core_tag",
        "backup": "core_backup",
        "badges": "core_badges",
        "grade": "core_grade",
        "message": "core_message",
        "question": "core_question",
        "availability": "core_availability",
        "contentbank": "core_contentbank",
        "payment": "core_payment",
        "media": "core_media",
    }
    if parts[0] in core_prefixes:
        return InferredComponent(core_prefixes[parts[0]], "core", parts[0])

    return InferredComponent("core", "core", parts[0])


def _infer_subplugin_component(
    parts: list[str],
    subplugin_mounts: Sequence[SubpluginMount],
) -> InferredComponent | None:
    """Return a child subplugin component when a path sits under a declared mount."""

    for mount in subplugin_mounts:
        mount_parts = [part for part in mount.mount_path.split("/") if part]
        if parts[: len(mount_parts)] != mount_parts:
            continue
        if len(parts) <= len(mount_parts):
            continue

        child_name = parts[len(mount_parts)]
        return _plugin_component(
            f"{mount.subtype}_{child_name}",
            mount.subtype,
            [*mount_parts, child_name],
        )

    return None

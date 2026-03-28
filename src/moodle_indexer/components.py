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

"""Moodle component inference from repository paths.

This module centralizes Moodle-specific path rules so the rest of the system can
reason in terms of component names instead of ad hoc path slicing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class InferredComponent:
    """The inferred Moodle component for a repository path."""

    name: str
    component_type: str
    root_path: str


def infer_component(relative_path: str) -> InferredComponent:
    """Infer the Moodle component from a repository-relative path."""

    parts = relative_path.split("/")
    if not parts:
        return InferredComponent("core", "core", ".")

    if len(parts) >= 2 and parts[0] in {"mod", "blocks", "local", "theme", "auth", "enrol", "message", "report", "repository", "question", "availability", "filter", "editor", "portfolio", "qtype", "qbank", "tool"}:
        if parts[0] == "tool":
            name = f"tool_{parts[1]}"
            return InferredComponent(name, "tool", "/".join(parts[:2]))
        name = f"{parts[0]}_{parts[1]}"
        return InferredComponent(name, parts[0], "/".join(parts[:2]))

    if len(parts) >= 3 and parts[0] == "admin" and parts[1] == "tool":
        return InferredComponent(f"tool_{parts[2]}", "tool", "/".join(parts[:3]))

    if len(parts) >= 3 and parts[0] == "course" and parts[1] == "format":
        return InferredComponent(f"format_{parts[2]}", "format", "/".join(parts[:3]))

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
        "grade": "core_grades",
        "message": "core_message",
    }
    if parts[0] in core_prefixes:
        return InferredComponent(core_prefixes[parts[0]], "core", parts[0])

    return InferredComponent("core", "core", parts[0])

# Copyright (c) Moodle Pty Ltd. All rights reserved.
# Licensed under the Moodle Community License v1.3.
# See LICENSE.md in the repository root for full terms.
# Commercial use requires a separate written agreement with Moodle.

"""Deterministic related-file suggestion heuristics.

The suggestion engine is intentionally simple and explainable so agentic tools
can understand why a file was recommended.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from moodle_indexer.components import infer_component
from moodle_indexer.file_roles import classify_file_role


@dataclass(slots=True)
class RelatedSuggestion:
    """A suggested companion file for a given context."""

    path: str
    reason: str


def suggest_related_files(relative_path: str) -> list[RelatedSuggestion]:
    """Return deterministic Moodle companion-file suggestions."""

    component = infer_component(relative_path)
    component_root = component.root_path
    file_name = Path(relative_path).name
    role = classify_file_role(relative_path)

    suggestions: list[RelatedSuggestion] = []
    seen_paths: set[str] = set()

    def add(path: str, reason: str) -> None:
        if path not in seen_paths and path != relative_path:
            seen_paths.add(path)
            suggestions.append(RelatedSuggestion(path=path, reason=reason))

    add(
        f"{component_root}/lang/en/{component.name}.php",
        f"{file_name} changes often require new or updated language strings for {component.name}.",
    )

    if role in {"settings_file", "lang_file"}:
        add(
            f"{component_root}/settings.php",
            "Settings-related work commonly involves the component settings definition file.",
        )
        add(
            "lib/adminlib.php",
            "suggested because settings.php uses Moodle admin settings APIs defined in lib/adminlib.php",
        )
        add(
            f"{component_root}/version.php",
            "Settings changes are often shipped alongside version bumps during Moodle development.",
        )

    if role in {"access_definition", "unknown", "lib_file", "locallib_file"}:
        add(
            f"{component_root}/db/access.php",
            "Capability-related work usually needs the component capability definition file.",
        )

    if role in {"install_xml", "upgrade_file"}:
        add(
            f"{component_root}/db/install.xml",
            "Schema work usually needs the install XML baseline.",
        )
        add(
            f"{component_root}/db/upgrade.php",
            "Schema changes usually need upgrade steps for existing installations.",
        )

    if role in {"renderer_file", "output_class", "template_file"}:
        add(
            f"{component_root}/classes/output",
            "Renderer and output changes often involve paired PHP output classes.",
        )
        add(
            f"{component_root}/templates",
            "Renderer changes commonly require matching Mustache templates.",
        )

    if role == "external_api_class":
        add(
            f"{component_root}/db/services.php",
            "External API classes are typically registered in db/services.php.",
        )
        add(
            f"{component_root}/classes/external",
            "External function work often spans related external API classes.",
        )

    if role == "task_class":
        add(
            f"{component_root}/db/tasks.php",
            "Scheduled task classes are declared in db/tasks.php.",
        )

    if relative_path.endswith(".php") and "/tests/" not in relative_path:
        add(
            f"{component_root}/tests",
            "Production code changes often need accompanying PHPUnit or Behat coverage.",
        )

    return sorted(suggestions, key=lambda item: (item.path, item.reason))

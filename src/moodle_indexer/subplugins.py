# Copyright (c) Moodle Pty Ltd. All rights reserved.
# Licensed under the Moodle Community License v1.3.
# See LICENSE.md in the repository root for full terms.
# Commercial use requires a separate written agreement with Moodle.

"""Subplugin discovery from Moodle ``db/subplugins.json`` files.

Moodle plugins can declare nested subplugin families whose files live inside
the parent plugin tree but belong to distinct logical components. This module
loads those declarations so indexing can attribute files such as
``mod/forum/report/summary/...`` to ``forumreport_summary`` instead of the
parent ``mod_forum`` component.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class SubpluginMount:
    """One declared subplugin mount rooted under a parent Moodle plugin."""

    subtype: str
    parent_component: str
    parent_root_path: str
    mount_path: str


def load_subplugin_mounts(application_root: Path) -> list[SubpluginMount]:
    """Load subplugin declarations visible under one Moodle application root."""

    from moodle_indexer.components import infer_component

    mounts: list[SubpluginMount] = []
    for descriptor_path in sorted(application_root.rglob("subplugins.json")):
        if descriptor_path.parent.name != "db":
            continue

        parent_root_path = descriptor_path.relative_to(application_root).parent.parent.as_posix()
        parent_component = infer_component(f"{parent_root_path}/version.php").name
        payload = json.loads(descriptor_path.read_text(encoding="utf-8"))

        declared_roots: dict[str, str] = {}
        for subtype, root_path in payload.get("plugintypes", {}).items():
            declared_roots[subtype] = _normalize_moodle_path(root_path)
        for subtype, folder_name in payload.get("subplugintypes", {}).items():
            declared_roots.setdefault(
                subtype,
                _normalize_moodle_path(f"{parent_root_path}/{folder_name}"),
            )

        for subtype, mount_path in sorted(declared_roots.items()):
            mounts.append(
                SubpluginMount(
                    subtype=subtype,
                    parent_component=parent_component,
                    parent_root_path=parent_root_path,
                    mount_path=mount_path,
                )
            )

    mounts.sort(key=lambda item: (-len(item.mount_path.split("/")), item.mount_path, item.subtype))
    return mounts


def _normalize_moodle_path(path: str) -> str:
    """Return a stable Moodle-style POSIX path."""

    normalized = Path(path).as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")

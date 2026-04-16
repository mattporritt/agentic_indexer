# Copyright (c) Moodle Pty Ltd. All rights reserved.
# Licensed under the Moodle Community License v1.3.
# See LICENSE.md in the repository root for full terms.
# Commercial use requires a separate written agreement with Moodle.

"""Utilities for repository-relative and Moodle-native paths.

Moodle indexing should be independent of the process working directory, so all
path transformations flow through this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from moodle_indexer.errors import ValidationError


@dataclass(slots=True)
class IndexedPaths:
    """Both path views stored for one indexed file."""

    repository_relative_path: str
    moodle_path: str
    path_scope: str


def normalize_relative_path(root: Path, file_path: Path) -> str:
    """Return a stable POSIX-style path relative to a chosen root."""

    resolved_root = root.resolve(strict=True)
    resolved_file = file_path.resolve(strict=True)
    try:
        return resolved_file.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        raise ValidationError(
            f"File path is outside the indexed repository root: {resolved_file}"
        ) from exc


def normalize_relative_lookup_path(file_path: str) -> str:
    """Normalize a user-supplied repository-relative path for DB lookup."""

    normalized = Path(file_path).as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def build_indexed_paths(repository_root: Path, application_root: Path, file_path: Path) -> IndexedPaths:
    """Build both repository-relative and Moodle-native paths for one file."""

    repository_relative_path = normalize_relative_path(repository_root, file_path)
    if _is_within(application_root, file_path):
        return IndexedPaths(
            repository_relative_path=repository_relative_path,
            moodle_path=normalize_relative_path(application_root, file_path),
            path_scope="application",
        )
    return IndexedPaths(
        repository_relative_path=repository_relative_path,
        moodle_path=repository_relative_path,
        path_scope="repository",
    )


def _is_within(root: Path, file_path: Path) -> bool:
    """Return whether ``file_path`` is inside ``root``."""

    try:
        file_path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except ValueError:
        return False

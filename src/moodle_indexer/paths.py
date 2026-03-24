"""Utilities for normalizing repository-relative paths.

Moodle indexing should be independent of the process working directory, so all
path transformations flow through this module.
"""

from __future__ import annotations

from pathlib import Path


def normalize_relative_path(root: Path, file_path: Path) -> str:
    """Return a stable POSIX-style path relative to the repository root."""

    return file_path.resolve().relative_to(root.resolve()).as_posix()


def resolve_user_file_input(root: Path, file_path: str) -> Path:
    """Resolve a CLI file input relative to the indexed Moodle root."""

    candidate = Path(file_path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (root / candidate).resolve()

"""Utilities for normalizing repository-relative paths.

Moodle indexing should be independent of the process working directory, so all
path transformations flow through this module.
"""

from __future__ import annotations

from pathlib import Path

from moodle_indexer.errors import ValidationError


def normalize_relative_path(root: Path, file_path: Path) -> str:
    """Return a stable POSIX-style path relative to the repository root.

    Both paths are resolved before comparison so the index stores one canonical
    repo-relative path shape regardless of the current working directory.
    """

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


def resolve_user_file_input(root: Path, file_path: str) -> Path:
    """Resolve a CLI file input relative to the indexed Moodle root."""

    candidate = Path(file_path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (root / candidate).resolve()

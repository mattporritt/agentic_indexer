"""Configuration helpers for CLI input and path validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from moodle_indexer.errors import ValidationError


@dataclass(slots=True)
class IndexConfig:
    """Top-level index build configuration."""

    moodle_root: Path
    database_path: Path


MOODLE_ROOT_MARKERS = ("admin", "lib", "mod")


def build_index_config(moodle_root: str, database_path: str) -> IndexConfig:
    """Validate CLI arguments and return a normalized index configuration.

    The CLI prefers the exact Moodle webroot. If the supplied path is a wrapper
    directory that contains a single obvious nested Moodle root such as
    ``public/``, the nested root is selected so persisted file paths remain
    repository-relative from the actual Moodle codebase.
    """

    root = _detect_effective_moodle_root(Path(moodle_root).expanduser())
    if not root.exists():
        raise ValidationError(f"Moodle path does not exist: {root}")
    if not root.is_dir():
        raise ValidationError(f"Moodle path is not a directory: {root}")

    db_path = Path(database_path).expanduser().resolve()
    if db_path.exists() and db_path.is_dir():
        raise ValidationError(f"Database path points to a directory: {db_path}")

    return IndexConfig(moodle_root=root, database_path=db_path)


def _detect_effective_moodle_root(candidate: Path) -> Path:
    """Return the actual Moodle root for a user-supplied path.

    This keeps the pipeline robust when a hosting checkout wraps Moodle one
    level deeper inside a directory such as ``public/``.
    """

    resolved = candidate.resolve()
    if _looks_like_moodle_root(resolved):
        return resolved

    child_candidates = [
        child.resolve()
        for child in resolved.iterdir()
        if child.is_dir() and _looks_like_moodle_root(child)
    ]
    if len(child_candidates) == 1:
        return child_candidates[0]
    return resolved


def _looks_like_moodle_root(path: Path) -> bool:
    """Return whether a directory looks like a Moodle codebase root."""

    return all((path / marker).exists() for marker in MOODLE_ROOT_MARKERS)

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


def build_index_config(moodle_root: str, database_path: str) -> IndexConfig:
    """Validate CLI arguments and return a normalized index configuration."""

    root = Path(moodle_root).expanduser().resolve()
    if not root.exists():
        raise ValidationError(f"Moodle path does not exist: {root}")
    if not root.is_dir():
        raise ValidationError(f"Moodle path is not a directory: {root}")

    db_path = Path(database_path).expanduser().resolve()
    if db_path.exists() and db_path.is_dir():
        raise ValidationError(f"Database path points to a directory: {db_path}")

    return IndexConfig(moodle_root=root, database_path=db_path)

# Copyright (c) Moodle Pty Ltd. All rights reserved.
# Licensed under the Moodle Community License v1.3.
# See LICENSE.md in the repository root for full terms.
# Commercial use requires a separate written agreement with Moodle.

"""Configuration helpers for CLI input and path validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from moodle_indexer.errors import ValidationError


@dataclass(slots=True)
class IndexConfig:
    """Top-level index build configuration."""

    input_path: str
    repository_root: Path
    application_root: Path
    layout_type: str
    database_path: Path
    workers: int = 1


APPLICATION_ROOT_MARKERS = ("admin", "mod", "theme")


def build_index_config(moodle_root: str, database_path: str, workers: int = 1) -> IndexConfig:
    """Validate CLI arguments and return a normalized index configuration.

    The repository root is always the exact checkout path supplied by the user.
    A separate application root is detected for split layouts such as Moodle
    5.1, where the main web application lives under ``public/``.
    """

    repository_root = Path(moodle_root).expanduser().resolve()
    if not repository_root.exists():
        raise ValidationError(f"Moodle path does not exist: {repository_root}")
    if not repository_root.is_dir():
        raise ValidationError(f"Moodle path is not a directory: {repository_root}")
    application_root, layout_type = detect_application_root(repository_root)

    db_path = Path(database_path).expanduser().resolve()
    if db_path.exists() and db_path.is_dir():
        raise ValidationError(f"Database path points to a directory: {db_path}")
    if workers < 1:
        raise ValidationError(f"Worker count must be at least 1, got: {workers}")

    return IndexConfig(
        input_path=moodle_root,
        repository_root=repository_root,
        application_root=application_root,
        layout_type=layout_type,
        database_path=db_path,
        workers=workers,
    )


def detect_application_root(repository_root: Path) -> tuple[Path, str]:
    """Return the application root and layout type for a repository.

    ``repository_root`` is never rewritten. In classic layouts the application
    root is the repository root; in split layouts it is ``repository_root /
    "public"``.
    """

    resolved = repository_root.resolve()
    if _looks_like_application_root(resolved):
        return resolved, "classic"

    public_root = (resolved / "public").resolve()
    if public_root.is_dir() and _looks_like_application_root(public_root):
        return public_root, "split_public"

    return resolved, "classic"


def _looks_like_application_root(path: Path) -> bool:
    """Return whether a directory looks like the main Moodle application root."""

    return all((path / marker).exists() for marker in APPLICATION_ROOT_MARKERS)

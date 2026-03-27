"""Repository scanning for files relevant to the Phase 1 index.

The scanner intentionally indexes a broad set of Moodle-friendly file types
while skipping common cache and VCS directories to keep rebuilds predictable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


SUPPORTED_EXTENSIONS = {".php", ".mustache", ".js", ".feature", ".xml"}
SKIP_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".idea",
    ".pytest_cache",
    "__pycache__",
    ".moodle-indexer",
}


@dataclass(slots=True)
class ScanResult:
    """Summary of repository discovery before extraction starts."""

    files: list[Path]
    ignored_files: int


def scan_repository(root: Path) -> ScanResult:
    """Return candidate files and lightweight scan diagnostics."""

    root = root.resolve(strict=True)
    files: list[Path] = []
    ignored_files = 0

    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRECTORIES)
        current_dir = Path(current_root)
        for filename in sorted(filenames):
            path = current_dir / filename
            if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                files.append(path)
            else:
                ignored_files += 1

    return ScanResult(files=files, ignored_files=ignored_files)

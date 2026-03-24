"""Repository scanning for files relevant to the Phase 1 index.

The scanner intentionally indexes a broad set of Moodle-friendly file types
while skipping common cache and VCS directories to keep rebuilds predictable.
"""

from __future__ import annotations

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


def scan_repository(root: Path) -> list[Path]:
    """Return sorted repository files considered for indexing."""

    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRECTORIES for part in path.parts):
            continue
        if path.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(path)
    return sorted(files)

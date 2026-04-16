# Copyright (c) Moodle Pty Ltd. All rights reserved.
# Licensed under the Moodle Community License v1.3.
# See LICENSE.md in the repository root for full terms.
# Commercial use requires a separate written agreement with Moodle.

"""Test configuration for the Moodle indexer package."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
CLASSIC_FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "moodle_sample"

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from moodle_indexer.config import build_index_config
from moodle_indexer.indexer import build_index
from moodle_indexer.store import open_database


@pytest.fixture(scope="session")
def classic_fixture_root() -> Path:
    """Return the classic Moodle fixture root used across integration tests."""

    return CLASSIC_FIXTURE_ROOT


@pytest.fixture(scope="session")
def classic_db_path(tmp_path_factory: pytest.TempPathFactory, classic_fixture_root: Path) -> Path:
    """Build one reusable classic-layout SQLite index for session-scoped tests."""

    db_dir = tmp_path_factory.mktemp("indexed-fixtures")
    db_path = db_dir / "classic.sqlite"
    build_index(build_index_config(str(classic_fixture_root), str(db_path), workers=2))
    return db_path


@pytest.fixture()
def classic_connection(classic_db_path: Path):
    """Open and close one SQLite connection for the shared classic fixture index."""

    connection = open_database(classic_db_path)
    try:
        yield connection
    finally:
        connection.close()

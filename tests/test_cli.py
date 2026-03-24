"""CLI-level tests for deterministic JSON output."""

from __future__ import annotations

import json
from pathlib import Path

from moodle_indexer.cli import main


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "moodle_sample"


def test_cli_index_and_file_context(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "index.sqlite"

    exit_code = main(
        [
            "index",
            "--moodle-path",
            str(FIXTURE_ROOT),
            "--db-path",
            str(db_path),
        ]
    )
    assert exit_code == 0
    index_payload = json.loads(capsys.readouterr().out)
    assert index_payload["status"] == "ok"
    assert index_payload["data"]["database"] == str(db_path.resolve())

    exit_code = main(
        [
            "file-context",
            "--db-path",
            str(db_path),
            "--moodle-path",
            str(FIXTURE_ROOT),
            "--file",
            "mod/forum/renderer.php",
        ]
    )
    assert exit_code == 0
    context_payload = json.loads(capsys.readouterr().out)
    assert context_payload["status"] == "ok"
    assert context_payload["data"]["file_role"] == "renderer_file"
    assert context_payload["data"]["component"] == "mod_forum"

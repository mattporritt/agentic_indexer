"""CLI-level tests for deterministic JSON output."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from moodle_indexer.cli import main


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "moodle_sample"
WRAPPER_PARENT_ROOT = Path(__file__).resolve().parent / "fixtures" / "hosting_wrapper"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_cli_index_and_file_context(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "index.sqlite"

    exit_code = main(
        [
            "index",
            "--moodle-path",
            str(FIXTURE_ROOT),
            "--db-path",
            str(db_path),
            "--workers",
            "2",
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    index_payload = json.loads(captured.out)
    assert index_payload["status"] == "ok"
    assert index_payload["data"]["database"] == str(db_path.resolve())
    assert "Indexing files" in captured.err

    exit_code = main(
        [
            "file-context",
            "--db-path",
            str(db_path),
            "--file",
            "mod/forum/renderer.php",
        ]
    )
    assert exit_code == 0
    context_payload = json.loads(capsys.readouterr().out)
    assert context_payload["status"] == "ok"
    assert context_payload["data"]["file_role"] == "renderer_file"
    assert context_payload["data"]["component"] == "mod_forum"
    assert context_payload["data"]["absolute_path"] == str((FIXTURE_ROOT / "mod/forum/renderer.php").resolve())
    assert "/public/" not in context_payload["data"]["absolute_path"]
    assert context_payload["data"]["string_usages"] == [
        {"component_name": "mod_forum", "line": 8, "string_key": "pluginname"}
    ]

    exit_code = main(
        [
            "find-symbol",
            "--db-path",
            str(db_path),
            "--symbol",
            "discussion_exporter",
        ]
    )
    assert exit_code == 0
    symbol_payload = json.loads(capsys.readouterr().out)
    assert symbol_payload["status"] == "ok"
    assert any(
        item["type"] == "extends" and item["target"] == "\\external_api"
        for item in symbol_payload["data"]["matches"][0]["relationships"]
    )


def test_python_module_index_command_persists_plugin_components_from_wrapper_root(tmp_path: Path) -> None:
    db_path = tmp_path / "cli-wrapper.sqlite"
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    src_path = str(PROJECT_ROOT / "src")
    env["PYTHONPATH"] = src_path if not existing_pythonpath else f"{src_path}{os.pathsep}{existing_pythonpath}"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "moodle_indexer",
            "index",
            "--moodle-path",
            str(WRAPPER_PARENT_ROOT),
            "--db-path",
            str(db_path),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == "ok"
    assert payload["data"]["repository"].endswith("tests/fixtures/hosting_wrapper/public")

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        components = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM components ORDER BY name"
            ).fetchall()
        }
        assert {"mod_forum", "mod_assign", "tool_phpunit", "theme_boost", "enrol_manual"} <= components

        forum_row = connection.execute(
            """
            SELECT files.relative_path, components.name AS component_name
            FROM files
            JOIN components ON components.id = files.component_id
            WHERE files.relative_path = 'mod/forum/lib.php'
            """
        ).fetchone()
        assert forum_row is not None
        assert forum_row["component_name"] == "mod_forum"
    finally:
        connection.close()


def test_file_context_cli_requires_only_db_path_and_file(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "index.sqlite"
    exit_code = main(
        [
            "index",
            "--moodle-path",
            str(FIXTURE_ROOT),
            "--db-path",
            str(db_path),
            "--workers",
            "2",
        ]
    )
    assert exit_code == 0
    capsys.readouterr()

    exit_code = main(
        [
            "file-context",
            "--db-path",
            str(db_path),
            "--file",
            "mod/forum/lib.php",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["data"]["file"] == "mod/forum/lib.php"
    assert payload["data"]["absolute_path"] == str((FIXTURE_ROOT / "mod/forum/lib.php").resolve())
    assert "/public/" not in payload["data"]["absolute_path"]


def test_index_cli_parallel_workers_persist_expected_components(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "parallel-index.sqlite"
    exit_code = main(
        [
            "index",
            "--moodle-path",
            str(WRAPPER_PARENT_ROOT),
            "--db-path",
            str(db_path),
            "--workers",
            "2",
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "ok"
    assert payload["data"]["components"] > 2
    assert "Indexing files" in captured.err

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        components = {
            row["name"]
            for row in connection.execute("SELECT name FROM components ORDER BY name").fetchall()
        }
        assert {"mod_forum", "mod_assign", "tool_phpunit", "theme_boost", "enrol_manual"} <= components
    finally:
        connection.close()

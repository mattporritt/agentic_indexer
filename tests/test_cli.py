"""CLI-level tests for deterministic JSON output."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path

from moodle_indexer.cli import main


CLASSIC_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "moodle_sample"
SPLIT_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "hosting_wrapper"
SPLIT_APP_ROOT = SPLIT_FIXTURE_ROOT / "public"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_cli_index_and_file_context_for_classic_layout(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "index.sqlite"

    exit_code = main(
        [
            "index",
            "--moodle-path",
            str(CLASSIC_FIXTURE_ROOT),
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
    assert index_payload["data"]["repository_root"] == str(CLASSIC_FIXTURE_ROOT.resolve())
    assert index_payload["data"]["application_root"] == str(CLASSIC_FIXTURE_ROOT.resolve())
    assert index_payload["data"]["layout_type"] == "classic"
    assert index_payload["data"]["discovered_files"] >= index_payload["data"]["processed_files"]
    assert index_payload["data"]["persisted_files"] == index_payload["data"]["processed_files"]
    assert index_payload["data"]["failed_files"] == 0
    assert index_payload["data"]["worker_usage"]["active_workers"] == 2
    assert "Scanning repository" in captured.err
    assert "Discovered" in captured.err
    assert "Parsing/extracting files" in captured.err
    assert "Persisting records" in captured.err

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
    assert context_payload["data"]["component"] == "mod_forum"
    assert context_payload["data"]["repository_relative_path"] == "mod/forum/renderer.php"
    assert context_payload["data"]["moodle_path"] == "mod/forum/renderer.php"
    assert context_payload["data"]["absolute_path"] == str((CLASSIC_FIXTURE_ROOT / "mod/forum/renderer.php").resolve())


def test_python_module_index_command_supports_split_layout_metadata(tmp_path: Path) -> None:
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
            str(SPLIT_FIXTURE_ROOT),
            "--db-path",
            str(db_path),
            "--workers",
            "2",
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
    assert payload["data"]["repository_root"] == str(SPLIT_FIXTURE_ROOT.resolve())
    assert payload["data"]["application_root"] == str(SPLIT_APP_ROOT.resolve())
    assert payload["data"]["layout_type"] == "split_public"

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        forum_row = connection.execute(
            """
            SELECT files.repository_relative_path, files.moodle_path, components.name AS component_name
            FROM files
            JOIN components ON components.id = files.component_id
            WHERE files.repository_relative_path = 'public/mod/forum/lib.php'
            """
        ).fetchone()
        assert forum_row is not None
        assert forum_row["moodle_path"] == "mod/forum/lib.php"
        assert forum_row["component_name"] == "mod_forum"
    finally:
        connection.close()


def test_packaging_declares_canonical_moodle_indexer_console_script() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["scripts"]["moodle-indexer"] == "moodle_indexer.cli:main"


def test_file_context_cli_uses_moodle_native_paths_for_split_layout(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "split.sqlite"
    exit_code = main(
        [
            "index",
            "--moodle-path",
            str(SPLIT_FIXTURE_ROOT),
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
    assert payload["data"]["repository_relative_path"] == "public/mod/forum/lib.php"
    assert payload["data"]["moodle_path"] == "mod/forum/lib.php"
    assert payload["data"]["absolute_path"] == str((SPLIT_APP_ROOT / "mod/forum/lib.php").resolve())

    exit_code = main(
        [
            "file-context",
            "--db-path",
            str(db_path),
            "--file",
            "admin/cli/install_database.php",
        ]
    )
    assert exit_code == 0
    root_payload = json.loads(capsys.readouterr().out)
    assert root_payload["data"]["repository_relative_path"] == "admin/cli/install_database.php"
    assert root_payload["data"]["moodle_path"] == "admin/cli/install_database.php"
    assert root_payload["data"]["absolute_path"] == str((SPLIT_FIXTURE_ROOT / "admin/cli/install_database.php").resolve())


def test_suggest_related_cli_uses_only_db_path_and_file(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "related.sqlite"
    exit_code = main(
        [
            "index",
            "--moodle-path",
            str(CLASSIC_FIXTURE_ROOT),
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
            "suggest-related",
            "--db-path",
            str(db_path),
            "--file",
            "mod/forum/db/access.php",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["data"]["file"] == "mod/forum/db/access.php"
    suggestions_by_path = {item["path"]: item for item in payload["data"]["suggestions"]}
    assert "mod/forum/lang/en/mod_forum.php" in suggestions_by_path
    assert suggestions_by_path["mod/forum/lang/en/mod_forum.php"]["indexed"] is True


def test_find_definition_cli_returns_ide_style_metadata(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "definitions.sqlite"
    exit_code = main(
        [
            "index",
            "--moodle-path",
            str(CLASSIC_FIXTURE_ROOT),
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
            "find-definition",
            "--db-path",
            str(db_path),
            "--symbol",
            "get_string",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["data"]["query"] == "get_string"
    match = payload["data"]["matches"][0]
    assert match["file"] == "lib/moodlelib.php"
    assert match["signature"] == "function get_string(string $identifier, ?string $component = null): string"
    assert match["docblock_summary"] == "Returns a localised string."

    exit_code = main(
        [
            "find-definition",
            "--db-path",
            str(db_path),
            "--symbol",
            "assign::view",
        ]
    )
    assert exit_code == 0
    method_payload = json.loads(capsys.readouterr().out)
    method_match = method_payload["data"]["matches"][0]
    assert method_match["class_name"] == "assign"
    assert method_match["inheritance_role"] == "interface_implementation"
    assert any(item["usage_kind"] == "instance_method_call" for item in method_match["usage_examples"])

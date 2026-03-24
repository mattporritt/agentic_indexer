"""End-to-end tests for the Phase 1 indexing and query flow."""

from __future__ import annotations

from pathlib import Path

from moodle_indexer.config import build_index_config
from moodle_indexer.indexer import build_index
from moodle_indexer.queries import component_summary, file_context, find_symbol, suggest_related
from moodle_indexer.store import open_database


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "moodle_sample"


def test_build_index_and_query_endpoints(tmp_path: Path) -> None:
    db_path = tmp_path / "moodle-index.sqlite"
    result = build_index(build_index_config(str(FIXTURE_ROOT), str(db_path)))

    assert result["files"] >= 10
    assert result["components"] >= 2
    assert result["symbols"] >= 8
    assert result["capabilities"] == 1
    assert result["language_strings"] >= 4
    assert result["tests"] >= 4

    connection = open_database(db_path)
    try:
        symbol_result = find_symbol(connection, "discussion_exporter")
        assert symbol_result["matches"]
        assert symbol_result["matches"][0]["component"] == "mod_forum"

        context_result = file_context(connection, FIXTURE_ROOT, "mod/forum/db/access.php")
        assert context_result["component"] == "mod_forum"
        assert context_result["file_role"] == "access_definition"
        assert context_result["capabilities"][0]["name"] == "mod/forum:viewdiscussion"

        component_result = component_summary(connection, "tool_demo")
        assert component_result["stats"]["language_string_count"] == 2
        assert component_result["key_file_roles"]["settings_file"] == 1

        related_result = suggest_related(connection, FIXTURE_ROOT, "admin/tool/demo/settings.php")
        paths = [item["path"] for item in related_result["suggestions"]]
        assert "admin/tool/demo/lang/en/tool_demo.php" in paths
        assert "admin/tool/demo/version.php" in paths
    finally:
        connection.close()

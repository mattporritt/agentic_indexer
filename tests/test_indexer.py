"""End-to-end and extractor-focused tests for the Phase 1 indexer."""

from __future__ import annotations

from pathlib import Path

from moodle_indexer.config import build_index_config, detect_application_root
from moodle_indexer.extractors import (
    extract_capabilities,
    extract_language_strings,
    extract_php_artifacts,
)
from moodle_indexer.indexer import build_index
from moodle_indexer.paths import build_indexed_paths, normalize_relative_lookup_path, normalize_relative_path
from moodle_indexer.queries import component_summary, file_context, find_symbol, suggest_related
from moodle_indexer.store import open_database


CLASSIC_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "moodle_sample"
SPLIT_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "hosting_wrapper"
SPLIT_APP_ROOT = SPLIT_FIXTURE_ROOT / "public"


def _read_fixture(relative_path: str) -> str:
    """Read one source fixture from the classic synthetic Moodle tree."""

    return (CLASSIC_FIXTURE_ROOT / relative_path).read_text(encoding="utf-8")


def test_path_helpers_support_repository_relative_and_moodle_native_shapes() -> None:
    classic_forum = CLASSIC_FIXTURE_ROOT / "mod" / "forum" / "lib.php"
    split_forum = SPLIT_APP_ROOT / "mod" / "forum" / "lib.php"
    split_cli = SPLIT_FIXTURE_ROOT / "admin" / "cli" / "install_database.php"

    assert normalize_relative_path(CLASSIC_FIXTURE_ROOT, classic_forum) == "mod/forum/lib.php"
    assert normalize_relative_lookup_path("./mod/forum/lib.php") == "mod/forum/lib.php"

    split_paths = build_indexed_paths(SPLIT_FIXTURE_ROOT, SPLIT_APP_ROOT, split_forum)
    assert split_paths.repository_relative_path == "public/mod/forum/lib.php"
    assert split_paths.moodle_path == "mod/forum/lib.php"
    assert split_paths.path_scope == "application"

    repository_only_paths = build_indexed_paths(SPLIT_FIXTURE_ROOT, SPLIT_APP_ROOT, split_cli)
    assert repository_only_paths.repository_relative_path == "admin/cli/install_database.php"
    assert repository_only_paths.moodle_path == "admin/cli/install_database.php"
    assert repository_only_paths.path_scope == "repository"


def test_detect_application_root_for_classic_and_split_layouts() -> None:
    classic_root, classic_layout = detect_application_root(CLASSIC_FIXTURE_ROOT)
    split_root, split_layout = detect_application_root(SPLIT_FIXTURE_ROOT)

    assert classic_root == CLASSIC_FIXTURE_ROOT.resolve()
    assert classic_layout == "classic"
    assert split_root == SPLIT_APP_ROOT.resolve()
    assert split_layout == "split_public"


def test_php_extraction_captures_structural_relationships() -> None:
    source = _read_fixture("mod/forum/classes/output/discussion_list.php")
    symbols, relationships = extract_php_artifacts(
        source,
        "mod/forum/classes/output/discussion_list.php",
        "mod_forum",
    )

    symbol_names = {(item.symbol_type, item.fqname) for item in symbols}
    assert ("class", "mod_forum\\output\\discussion_list") in symbol_names
    assert ("method", "mod_forum\\output\\discussion_list::export_for_template") in symbol_names

    relationship_pairs = {
        (item.relationship_type, item.source_fqname, item.target_name)
        for item in relationships
    }
    assert (
        "implements",
        "mod_forum\\output\\discussion_list",
        "renderable",
    ) in relationship_pairs
    assert (
        "defines_method",
        "mod_forum\\output\\discussion_list",
        "export_for_template",
    ) in relationship_pairs
    assert (
        "method_of",
        "mod_forum\\output\\discussion_list::export_for_template",
        "mod_forum\\output\\discussion_list",
    ) in relationship_pairs


def test_capability_and_language_string_extractors_capture_metadata() -> None:
    capability_source = _read_fixture("mod/forum/db/access.php")
    capabilities = extract_capabilities(capability_source, "mod/forum/db/access.php", "mod_forum")
    assert len(capabilities) == 1
    assert capabilities[0].name == "mod/forum:viewdiscussion"
    assert capabilities[0].captype == "read"
    assert capabilities[0].contextlevel == "CONTEXT_MODULE"
    assert capabilities[0].archetypes == {
        "editingteacher": "CAP_ALLOW",
        "student": "CAP_ALLOW",
    }
    assert capabilities[0].riskbitmask == "RISK_SPAM"

    string_source = _read_fixture("mod/forum/lang/en/mod_forum.php")
    strings = extract_language_strings(string_source, "mod/forum/lang/en/mod_forum.php", "mod_forum")
    assert [item.string_key for item in strings] == ["pluginname", "privacy:metadata"]
    assert strings[0].string_value == "Forum"


def test_classic_layout_indexing_and_queries(tmp_path: Path) -> None:
    db_path = tmp_path / "classic.sqlite"
    result = build_index(build_index_config(str(CLASSIC_FIXTURE_ROOT), str(db_path), workers=2))

    assert result["input_path"] == str(CLASSIC_FIXTURE_ROOT)
    assert result["repository_root"] == str(CLASSIC_FIXTURE_ROOT.resolve())
    assert result["application_root"] == str(CLASSIC_FIXTURE_ROOT.resolve())
    assert result["layout_type"] == "classic"
    assert result["components"] >= 5
    assert result["relationships"] >= 6

    connection = open_database(db_path)
    try:
        repository = connection.execute(
            "SELECT input_path, repository_root, application_root, layout_type FROM repositories"
        ).fetchone()
        assert repository["input_path"] == str(CLASSIC_FIXTURE_ROOT)
        assert repository["repository_root"] == str(CLASSIC_FIXTURE_ROOT.resolve())
        assert repository["application_root"] == str(CLASSIC_FIXTURE_ROOT.resolve())
        assert repository["layout_type"] == "classic"

        mod_forum_row = connection.execute(
            """
            SELECT f.repository_relative_path, f.moodle_path, c.name AS component_name
            FROM files f
            JOIN components c ON c.id = f.component_id
            WHERE f.repository_relative_path = 'mod/forum/lib.php'
            """
        ).fetchone()
        assert mod_forum_row["moodle_path"] == "mod/forum/lib.php"
        assert mod_forum_row["component_name"] == "mod_forum"

        stored_components = {
            row["name"]
            for row in connection.execute("SELECT name FROM components ORDER BY name").fetchall()
        }
        assert {"mod_forum", "tool_demo", "theme_boost", "enrol_manual", "tool_phpunit"} <= stored_components

        symbol_result = find_symbol(connection, "discussion_exporter")
        assert symbol_result["matches"][0]["file"] == "mod/forum/classes/external/discussion_exporter.php"
        assert symbol_result["matches"][0]["repository_relative_path"] == "mod/forum/classes/external/discussion_exporter.php"

        access_context = file_context(connection, "mod/forum/db/access.php")
        assert access_context["component"] == "mod_forum"
        assert access_context["repository_relative_path"] == "mod/forum/db/access.php"
        assert access_context["moodle_path"] == "mod/forum/db/access.php"
        assert access_context["path_scope"] == "application"
        assert access_context["absolute_path"] == str((CLASSIC_FIXTURE_ROOT / "mod/forum/db/access.php").resolve())
        assert access_context["capabilities"][0]["archetypes"]["student"] == "CAP_ALLOW"

        component_result = component_summary(connection, "mod_forum")
        assert component_result["stats"]["relationship_count"] >= 6
        assert any(
            file_row["repository_relative_path"] == "mod/forum/lib.php"
            and file_row["moodle_path"] == "mod/forum/lib.php"
            for file_row in component_result["files"]
        )

        related_result = suggest_related(connection, CLASSIC_FIXTURE_ROOT, "admin/tool/demo/settings.php")
        suggestions_by_path = {item["path"]: item for item in related_result["suggestions"]}
        assert suggestions_by_path["admin/tool/demo/lang/en/tool_demo.php"]["indexed"] is True
    finally:
        connection.close()


def test_split_layout_indexes_whole_repository_and_supports_moodle_native_queries(tmp_path: Path) -> None:
    db_path = tmp_path / "split.sqlite"
    result = build_index(build_index_config(str(SPLIT_FIXTURE_ROOT), str(db_path), workers=2))

    assert result["input_path"] == str(SPLIT_FIXTURE_ROOT)
    assert result["repository_root"] == str(SPLIT_FIXTURE_ROOT.resolve())
    assert result["application_root"] == str(SPLIT_APP_ROOT.resolve())
    assert result["layout_type"] == "split_public"
    assert result["components"] > 2

    connection = open_database(db_path)
    try:
        repository = connection.execute(
            "SELECT input_path, repository_root, application_root, layout_type FROM repositories"
        ).fetchone()
        assert repository["repository_root"] == str(SPLIT_FIXTURE_ROOT.resolve())
        assert repository["application_root"] == str(SPLIT_APP_ROOT.resolve())
        assert repository["layout_type"] == "split_public"

        stored_paths = {
            tuple(row)
            for row in connection.execute(
                "SELECT repository_relative_path, moodle_path, path_scope FROM files ORDER BY repository_relative_path"
            ).fetchall()
        }
        assert ("public/mod/forum/lib.php", "mod/forum/lib.php", "application") in stored_paths
        assert ("public/theme/boost/lib.php", "theme/boost/lib.php", "application") in stored_paths
        assert ("public/enrol/manual/lib.php", "enrol/manual/lib.php", "application") in stored_paths
        assert ("public/admin/tool/phpunit/index.php", "admin/tool/phpunit/index.php", "application") in stored_paths
        assert ("admin/cli/install_database.php", "admin/cli/install_database.php", "repository") in stored_paths
        assert ("lib/classes/some_core_class.php", "lib/classes/some_core_class.php", "repository") in stored_paths

        assert connection.execute(
            "SELECT COUNT(*) AS count FROM components"
        ).fetchone()["count"] > 2

        forum_row = connection.execute(
            """
            SELECT f.repository_relative_path, f.moodle_path, c.name AS component_name
            FROM files f
            JOIN components c ON c.id = f.component_id
            WHERE f.repository_relative_path = 'public/mod/forum/lib.php'
            """
        ).fetchone()
        assert forum_row["moodle_path"] == "mod/forum/lib.php"
        assert forum_row["component_name"] == "mod_forum"

        phpunit_row = connection.execute(
            """
            SELECT c.name AS component_name
            FROM files f
            JOIN components c ON c.id = f.component_id
            WHERE f.repository_relative_path = 'public/admin/tool/phpunit/index.php'
            """
        ).fetchone()
        assert phpunit_row["component_name"] == "tool_phpunit"

        cli_row = connection.execute(
            """
            SELECT c.name AS component_name
            FROM files f
            JOIN components c ON c.id = f.component_id
            WHERE f.repository_relative_path = 'admin/cli/install_database.php'
            """
        ).fetchone()
        assert cli_row["component_name"] == "core_admin"

        split_context = file_context(connection, "mod/forum/lib.php")
        assert split_context["repository_relative_path"] == "public/mod/forum/lib.php"
        assert split_context["moodle_path"] == "mod/forum/lib.php"
        assert split_context["path_scope"] == "application"
        assert split_context["absolute_path"] == str((SPLIT_APP_ROOT / "mod/forum/lib.php").resolve())

        repo_context = file_context(connection, "admin/cli/install_database.php")
        assert repo_context["repository_relative_path"] == "admin/cli/install_database.php"
        assert repo_context["moodle_path"] == "admin/cli/install_database.php"
        assert repo_context["path_scope"] == "repository"
        assert repo_context["absolute_path"] == str((SPLIT_FIXTURE_ROOT / "admin/cli/install_database.php").resolve())

        repository_relative_lookup = file_context(connection, "public/mod/forum/lib.php")
        assert repository_relative_lookup["moodle_path"] == "mod/forum/lib.php"
        assert repository_relative_lookup["repository_relative_path"] == "public/mod/forum/lib.php"
    finally:
        connection.close()

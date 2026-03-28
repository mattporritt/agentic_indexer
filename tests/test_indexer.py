"""End-to-end and extractor-focused tests for the Phase 1 indexer."""

from __future__ import annotations

from pathlib import Path

from moodle_indexer.config import build_index_config, detect_application_root
from moodle_indexer.extractors import (
    extract_capabilities,
    extract_language_strings,
    extract_php_artifacts,
    extract_webservices,
)
from moodle_indexer import indexer as indexer_module
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
    capability_map = {item.name: item for item in capabilities}
    assert set(capability_map) == {
        "mod/forum:addinstance",
        "mod/forum:viewdiscussion",
        "mod/forum:replypost",
        "mod/forum:startdiscussion",
        "mod/forum:deleteownpost",
        "mod/forum:exportdiscussion",
        "mod/forum:grade",
        "mod/forum:canmailnow",
    }
    assert "archetypes" not in capability_map
    assert capability_map["mod/forum:viewdiscussion"].captype == "read"
    assert capability_map["mod/forum:viewdiscussion"].contextlevel == "CONTEXT_MODULE"
    assert capability_map["mod/forum:viewdiscussion"].archetypes == {
        "editingteacher": "CAP_ALLOW",
        "student": "CAP_ALLOW",
    }
    assert capability_map["mod/forum:viewdiscussion"].riskbitmask == "RISK_SPAM"
    assert capability_map["mod/forum:addinstance"].clonepermissionsfrom == "moodle/course:manageactivities"

    string_source = _read_fixture("mod/forum/lang/en/mod_forum.php")
    strings = extract_language_strings(string_source, "mod/forum/lang/en/mod_forum.php", "mod_forum")
    assert [item.string_key for item in strings] == ["pluginname", "privacy:metadata"]
    assert strings[0].string_value == "Forum"


def test_webservice_extractor_resolves_classpath_and_classname_targets() -> None:
    source = _read_fixture("mod/assign/db/services.php")
    webservices = extract_webservices(source, "mod/assign/db/services.php", "mod_assign")

    service_map = {item.service_name: item for item in webservices}
    assert set(service_map) == {
        "mod_assign_remove_submission",
        "mod_assign_start_submission",
        "mod_assign_submit_grading_form",
    }
    assert service_map["mod_assign_submit_grading_form"].classpath == "mod/assign/externallib.php"
    assert service_map["mod_assign_submit_grading_form"].resolved_target_file == "mod/assign/externallib.php"
    assert service_map["mod_assign_submit_grading_form"].resolution_type == "classpath"
    assert service_map["mod_assign_remove_submission"].classname == "mod_assign\\external\\remove_submission"
    assert service_map["mod_assign_remove_submission"].resolved_target_file == "mod/assign/classes/external/remove_submission.php"
    assert service_map["mod_assign_remove_submission"].resolution_type == "classname"
    assert service_map["mod_assign_start_submission"].classname == "mod_assign\\external\\start_submission"
    assert service_map["mod_assign_start_submission"].resolved_target_file == "mod/assign/classes/external/start_submission.php"
    assert service_map["mod_assign_start_submission"].resolution_type == "classname"


def test_classic_layout_indexing_and_queries(tmp_path: Path) -> None:
    db_path = tmp_path / "classic.sqlite"
    result = build_index(build_index_config(str(CLASSIC_FIXTURE_ROOT), str(db_path), workers=2))

    assert result["input_path"] == str(CLASSIC_FIXTURE_ROOT)
    assert result["repository_root"] == str(CLASSIC_FIXTURE_ROOT.resolve())
    assert result["application_root"] == str(CLASSIC_FIXTURE_ROOT.resolve())
    assert result["layout_type"] == "classic"
    assert result["components"] >= 6
    assert result["relationships"] >= 6
    assert result["discovered_files"] >= result["processed_files"] == result["files"]
    assert result["persisted_files"] == result["processed_files"]
    assert result["failed_files"] == 0
    assert result["skipped_files"] == 0
    assert result["worker_usage"]["requested_workers"] == 2
    assert result["worker_usage"]["mode"] == "parallel"
    assert result["worker_usage"]["tasks_submitted"] == result["discovered_files"]
    assert result["timings"]["scan_seconds"] >= 0
    assert result["timings"]["pipeline_seconds"] >= 0
    assert result["timings"]["persistence_seconds"] >= 0
    assert result["timings"]["total_seconds"] >= 0

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
        assert {
            "mod_forum",
            "forumreport_summary",
            "mod_assign",
            "tool_demo",
            "theme_boost",
            "enrol_manual",
            "tool_phpunit",
        } <= stored_components

        symbol_result = find_symbol(connection, "discussion_exporter")
        assert symbol_result["matches"][0]["file"] == "mod/forum/classes/external/discussion_exporter.php"
        assert symbol_result["matches"][0]["repository_relative_path"] == "mod/forum/classes/external/discussion_exporter.php"

        access_context = file_context(connection, "mod/forum/db/access.php")
        assert access_context["component"] == "mod_forum"
        assert access_context["repository_relative_path"] == "mod/forum/db/access.php"
        assert access_context["moodle_path"] == "mod/forum/db/access.php"
        assert access_context["path_scope"] == "application"
        assert access_context["absolute_path"] == str((CLASSIC_FIXTURE_ROOT / "mod/forum/db/access.php").resolve())
        access_capability_names = [item["name"] for item in access_context["capabilities"]]
        assert "mod/forum:addinstance" in access_capability_names
        assert "forumreport/summary:view" not in access_capability_names
        addinstance = next(item for item in access_context["capabilities"] if item["name"] == "mod/forum:addinstance")
        assert addinstance["clonepermissionsfrom"] == "moodle/course:manageactivities"

        component_result = component_summary(connection, "mod_forum")
        assert component_result["stats"]["relationship_count"] >= 6
        assert any(
            file_row["repository_relative_path"] == "mod/forum/lib.php"
            and file_row["moodle_path"] == "mod/forum/lib.php"
            for file_row in component_result["files"]
        )
        mod_forum_capabilities = {item["name"] for item in component_result["capabilities"]}
        assert {
            "mod/forum:addinstance",
            "mod/forum:viewdiscussion",
            "mod/forum:replypost",
            "mod/forum:startdiscussion",
            "mod/forum:deleteownpost",
            "mod/forum:exportdiscussion",
            "mod/forum:grade",
            "mod/forum:canmailnow",
        } <= mod_forum_capabilities
        assert "forumreport/summary:view" not in mod_forum_capabilities

        child_summary = component_summary(connection, "forumreport_summary")
        child_capabilities = {item["name"] for item in child_summary["capabilities"]}
        assert child_capabilities == {
            "forumreport/summary:view",
            "forumreport/summary:viewall",
        }
        assert any(
            file_row["repository_relative_path"] == "mod/forum/report/summary/db/access.php"
            for file_row in child_summary["files"]
        )

        child_context = file_context(connection, "mod/forum/report/summary/db/access.php")
        assert child_context["component"] == "forumreport_summary"
        assert {item["name"] for item in child_context["capabilities"]} == {
            "forumreport/summary:view",
            "forumreport/summary:viewall",
        }

        assign_summary = component_summary(connection, "mod_assign")
        assert {item["service_name"] for item in assign_summary["webservices"]} == {
            "mod_assign_remove_submission",
            "mod_assign_start_submission",
            "mod_assign_submit_grading_form",
        }
        service_resolution_types = {
            item["service_name"]: item["resolution_type"]
            for item in assign_summary["webservices"]
        }
        assert service_resolution_types["mod_assign_submit_grading_form"] == "classpath"
        assert service_resolution_types["mod_assign_start_submission"] == "classname"

        services_context = file_context(connection, "mod/assign/db/services.php")
        assert {item["service_name"] for item in services_context["webservices"]} == {
            "mod_assign_remove_submission",
            "mod_assign_start_submission",
            "mod_assign_submit_grading_form",
        }
        assert {
            item["resolved_target_file"] for item in services_context["webservices"]
        } == {
            "mod/assign/externallib.php",
            "mod/assign/classes/external/remove_submission.php",
            "mod/assign/classes/external/start_submission.php",
        }
        linked_test_files = {item["file"] for item in services_context["tests"]}
        assert linked_test_files == {
            "mod/assign/tests/external/remove_submission_test.php",
            "mod/assign/tests/external/start_submission_test.php",
            "mod/assign/tests/externallib_test.php",
            "mod/assign/tests/externallib_advanced_testcase.php",
        }
        linked_test_reasons = {item["file"]: item["reason"] for item in services_context["tests"]}
        assert "service class mod_assign\\external\\start_submission" in linked_test_reasons[
            "mod/assign/tests/external/start_submission_test.php"
        ]
        assert "externallib.php changes are often covered" in linked_test_reasons[
            "mod/assign/tests/externallib_test.php"
        ]
        service_related_paths = {item["path"] for item in services_context["related_suggestions"]}
        assert "mod/assign/externallib.php" in service_related_paths
        assert "mod/assign/classes/external/start_submission.php" in service_related_paths
        assert "mod/assign/tests/external/start_submission_test.php" in service_related_paths
        assert "mod/assign/tests/externallib_test.php" in service_related_paths

        assign_related = suggest_related(connection, "mod/assign/db/services.php")
        assign_suggestions = {item["path"]: item for item in assign_related["suggestions"]}
        assert "mod/assign/externallib.php" in assign_suggestions
        assert "classpath" in assign_suggestions["mod/assign/externallib.php"]["reason"]
        assert "mod/assign/classes/external/start_submission.php" in assign_suggestions
        assert "mod_assign\\external\\start_submission" in assign_suggestions[
            "mod/assign/classes/external/start_submission.php"
        ]["reason"]
        assert "mod/assign/tests/external/remove_submission_test.php" in assign_suggestions
        assert "mod_assign\\external\\remove_submission" in assign_suggestions[
            "mod/assign/tests/external/remove_submission_test.php"
        ]["reason"]
        assert "mod/assign/tests/external/start_submission_test.php" in assign_suggestions
        assert "mod/assign/tests/externallib_test.php" in assign_suggestions
        assert "externallib.php changes are often covered" in assign_suggestions[
            "mod/assign/tests/externallib_test.php"
        ]["reason"]
        assert "mod/assign/tests/externallib_advanced_testcase.php" in assign_suggestions
        assert "shared web service test coverage" in assign_suggestions[
            "mod/assign/tests/externallib_advanced_testcase.php"
        ]["reason"]

        related_result = suggest_related(connection, "admin/tool/demo/settings.php")
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
    assert result["failed_files"] == 0
    assert result["persisted_files"] == result["processed_files"] == result["files"]

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


def test_index_summary_reports_failed_files_without_aborting(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "failure.sqlite"
    original = indexer_module._process_file_for_indexing

    def flaky_process(repository_root: Path, application_root: Path, file_path: Path, subplugin_mounts) -> dict:
        if file_path.name == "renderer.php":
            raise RuntimeError("synthetic extraction failure")
        return original(repository_root, application_root, file_path, subplugin_mounts)

    monkeypatch.setattr(indexer_module, "_process_file_for_indexing", flaky_process)

    result = build_index(build_index_config(str(CLASSIC_FIXTURE_ROOT), str(db_path), workers=2))

    assert result["discovered_files"] > 0
    assert result["failed_files"] == 1
    assert result["processed_files"] == result["discovered_files"] - 1
    assert result["persisted_files"] == result["processed_files"]
    assert result["failure_examples"][0]["file"].endswith("mod/forum/renderer.php")
    assert "synthetic extraction failure" in result["failure_examples"][0]["error"]

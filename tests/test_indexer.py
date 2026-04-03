"""End-to-end and extractor-focused tests for the Phase 1 indexer."""

from __future__ import annotations

from pathlib import Path

from moodle_indexer.config import build_index_config, detect_application_root
from moodle_indexer.extractors import (
    extract_capabilities,
    extract_js_module_artifacts,
    extract_language_strings,
    extract_php_artifacts,
    extract_webservices,
)
from moodle_indexer import indexer as indexer_module
from moodle_indexer.indexer import build_index
from moodle_indexer.js_modules import resolve_js_module
from moodle_indexer.paths import build_indexed_paths, normalize_relative_lookup_path, normalize_relative_path
from moodle_indexer.php_parser import ParsedMethod, ParsedSymbol, _merge_symbol_metadata
from moodle_indexer.queries import component_summary, file_context, find_definition, find_symbol, suggest_related
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


def test_merge_symbol_metadata_preserves_regex_only_legacy_methods() -> None:
    parsed_symbols = [
        ParsedSymbol(
            symbol_type="class",
            name="assign",
            fqname="assign",
            line=17,
            methods=[
                ParsedMethod(name="__construct", line=129),
                ParsedMethod(name="set_is_marking", line=258),
            ],
        )
    ]
    regex_symbols = [
        ParsedSymbol(
            symbol_type="class",
            name="assign",
            fqname="assign",
            line=17,
            methods=[
                ParsedMethod(name="__construct", line=129),
                ParsedMethod(name="set_is_marking", line=258),
                ParsedMethod(name="view", line=400, signature="public function view(): string"),
            ],
        )
    ]

    merged = _merge_symbol_metadata(parsed_symbols, regex_symbols)

    assert len(merged) == 1
    assert {method.name for method in merged[0].methods} == {"__construct", "set_is_marking", "view"}


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


def test_js_module_extractor_handles_modern_and_legacy_moodle_patterns() -> None:
    modern_source = _read_fixture("ai/amd/src/aiprovider_action_management_table.js")
    modern_module, modern_imports, modern_relationships = extract_js_module_artifacts(
        modern_source,
        "ai/amd/src/aiprovider_action_management_table.js",
        "core_ai",
    )

    assert modern_module is not None
    assert modern_module.module_name == "core_ai/aiprovider_action_management_table"
    assert modern_module.export_kind == "default_class"
    assert modern_module.superclass_name == "PluginManagementTable"
    assert modern_module.superclass_module == "core_admin/plugin_management_table"
    assert modern_module.resolved_superclass_file == "admin/amd/src/plugin_management_table.js"
    assert modern_module.build_file == "ai/amd/build/aiprovider_action_management_table.min.js"
    modern_import_map = {(item.module_name, item.local_name, item.import_kind) for item in modern_imports}
    assert ("core_admin/plugin_management_table", "PluginManagementTable", "default") in modern_import_map
    assert ("core/ajax", "fetchMany", "named") in modern_import_map
    assert ("core_ai/local_actions", "buildActionPayload", "named") in modern_import_map
    assert ("js_extends", "core_admin/plugin_management_table") in {
        (item.relationship_type, item.target_name) for item in modern_relationships
    }
    assert ("builds_to", "ai/amd/build/aiprovider_action_management_table.min.js") in {
        (item.relationship_type, item.target_name) for item in modern_relationships
    }

    legacy_source = _read_fixture("mod/forum/amd/src/forum.js")
    legacy_module, legacy_imports, legacy_relationships = extract_js_module_artifacts(
        legacy_source,
        "mod/forum/amd/src/forum.js",
        "mod_forum",
    )

    assert legacy_module is not None
    assert legacy_module.module_name == "mod_forum/forum"
    assert legacy_module.export_kind == "amd_return_object"
    legacy_import_map = {(item.module_name, item.local_name, item.resolved_target_file) for item in legacy_imports}
    assert ("jquery", "$", None) in legacy_import_map
    assert ("core/ajax", "Ajax", "lib/amd/src/ajax.js") in legacy_import_map
    assert ("mod_forum/repository", "Repository", "mod/forum/amd/src/repository.js") in legacy_import_map
    assert ("builds_to", "mod/forum/amd/build/forum.min.js") in {
        (item.relationship_type, item.target_name) for item in legacy_relationships
    }


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
            "core_ai",
            "aiprovider_openai",
            "mod_forum",
            "forumreport_summary",
            "mod_assign",
            "tool_demo",
            "tool_mfa",
            "theme_boost",
            "enrol_manual",
            "tool_phpunit",
        } <= stored_components

        js_registry_entries = {
            (row["module_name"], row["moodle_path"], row["build_file"])
            for row in connection.execute(
                """
                SELECT jm.module_name, f.moodle_path, jm.build_file
                FROM js_modules jm
                JOIN files f ON f.id = jm.file_id
                ORDER BY jm.module_name
                """
            ).fetchall()
        }
        assert ("core/ajax", "lib/amd/src/ajax.js", "lib/amd/build/ajax.min.js") in js_registry_entries
        assert (
            "core_admin/plugin_management_table",
            "admin/amd/src/plugin_management_table.js",
            "admin/amd/build/plugin_management_table.min.js",
        ) in js_registry_entries
        assert (
            "core_ai/aiprovider_action_management_table",
            "ai/amd/src/aiprovider_action_management_table.js",
            "ai/amd/build/aiprovider_action_management_table.min.js",
        ) in js_registry_entries
        assert ("mod_forum/repository", "mod/forum/amd/src/repository.js", "mod/forum/amd/build/repository.min.js") in js_registry_entries

        assert resolve_js_module(connection, "core/ajax").resolution_strategy == "indexed_registry"
        assert resolve_js_module(connection, "core_admin/plugin_management_table").source_file == "admin/amd/src/plugin_management_table.js"
        assert resolve_js_module(connection, "mod_forum/repository").source_file == "mod/forum/amd/src/repository.js"
        jquery_resolution = resolve_js_module(connection, "jquery")
        assert jquery_resolution.resolution_status == "external"
        assert jquery_resolution.resolution_strategy == "external_runtime"
        assert jquery_resolution.is_external is True

        get_string_definition = find_definition(connection, "get_string")
        assert get_string_definition["total_matches"] == 1
        get_string_match = get_string_definition["matches"][0]
        assert get_string_match["symbol_type"] == "function"
        assert get_string_match["file"] == "lib/moodlelib.php"
        assert get_string_match["component"] == "core"
        assert get_string_match["signature"] == "function get_string(string $identifier, ?string $component = null): string"
        assert get_string_match["parameters"] == [
            {"name": "identifier", "type": "string", "default": None},
            {"name": "component", "type": "?string", "default": "null"},
        ]
        assert get_string_match["return_type"] == "string"
        assert get_string_match["docblock_summary"] == "Returns a localised string."
        assert any(example["usage_kind"] == "function_call" for example in get_string_match["usage_examples"])

        assign_view_definition = find_definition(connection, "assign::view")
        assert assign_view_definition["total_matches"] == 1
        assign_view_match = assign_view_definition["matches"][0]
        assert assign_view_match["symbol_type"] == "method"
        assert assign_view_match["class_name"] == "assign"
        assert assign_view_match["file"] == "mod/assign/locallib.php"
        assert assign_view_match["visibility"] == "public"
        assert assign_view_match["is_static"] is False
        assert assign_view_match["is_final"] is False
        assert assign_view_match["is_abstract"] is False
        assert assign_view_match["return_type"] == "string"
        assert assign_view_match["docblock_summary"] == "Render the current assignment view."
        assert assign_view_match["inheritance_role"] == "override"
        assert assign_view_match["overrides"] == "mod_assign\\local\\assign_base::view"
        assert assign_view_match["implements_method"] == ["mod_assign\\local\\viewable::view"]
        assert assign_view_match["matched_via"] == "direct_definition"
        assert assign_view_match["parent_definition"]["fqname"] == "mod_assign\\local\\assign_base::view"
        assert assign_view_match["overrides_definition"]["fqname"] == "mod_assign\\local\\assign_base::view"
        assert len(assign_view_match["implements_definitions"]) == 1
        implements_definition = assign_view_match["implements_definitions"][0]
        assert implements_definition["fqname"] == "mod_assign\\local\\viewable::view"
        assert implements_definition["file"] == "mod/assign/classes/local/viewable.php"
        assert {example["file"] for example in assign_view_match["usage_examples"]} == {
            "mod/assign/externallib.php",
            "mod/assign/renderer.php",
        }
        assert all("->view(" in example["snippet"] for example in assign_view_match["usage_examples"])
        assert assign_view_match["usage_summary"] == {"instance_method_call": 1, "renderer_usage": 1}

        assign_view_with_leading_slash = find_definition(connection, "\\assign::view")
        assert assign_view_with_leading_slash["total_matches"] == 1
        assert assign_view_with_leading_slash["matches"][0]["fqname"] == assign_view_match["fqname"]

        base_view_definition = find_definition(connection, "mod_assign\\local\\assign_base::view")
        assert base_view_definition["total_matches"] == 1
        base_view_match = base_view_definition["matches"][0]
        assert base_view_match["inheritance_role"] == "base_definition"
        assert {item["fqname"] for item in base_view_match["child_overrides"]} >= {"assign::view"}

        interface_view_definition = find_definition(connection, "mod_assign\\local\\viewable::view")
        assert interface_view_definition["total_matches"] == 1
        interface_view_match = interface_view_definition["matches"][0]
        assert interface_view_match["inheritance_role"] == "base_definition"
        assert {item["fqname"] for item in interface_view_match["child_overrides"]} >= {
            "assign::view",
            "mod_assign\\local\\simple_view::view",
        }

        simple_view_definition = find_definition(connection, "mod_assign\\local\\simple_view::view")
        assert simple_view_definition["total_matches"] == 1
        simple_view_match = simple_view_definition["matches"][0]
        assert simple_view_match["inheritance_role"] == "interface_implementation"
        assert simple_view_match["overrides"] is None
        assert simple_view_match["implements_method"] == ["mod_assign\\local\\viewable::view"]
        assert simple_view_match["implements_definitions"][0]["fqname"] == "mod_assign\\local\\viewable::view"

        inherited_view_definition = find_definition(connection, "mod_assign\\local\\passive_assign::view")
        assert inherited_view_definition["total_matches"] == 1
        inherited_view_match = inherited_view_definition["matches"][0]
        assert inherited_view_match["inheritance_role"] == "inherited_not_overridden"
        assert inherited_view_match["matched_via"] == "inherited_definition"
        assert inherited_view_match["requested_class_name"] == "mod_assign\\local\\passive_assign"
        assert inherited_view_match["fqname"] == "mod_assign\\local\\assign_base::view"
        assert inherited_view_match["parent_definition"]["fqname"] == "mod_assign\\local\\assign_base::view"

        delete_instance_definition = find_definition(connection, "assign::delete_instance")
        assert delete_instance_definition["total_matches"] == 1
        delete_instance_match = delete_instance_definition["matches"][0]
        assert delete_instance_match["signature"] == (
            "public function delete_instance(array $options = array('force' => true)): bool"
        )
        assert delete_instance_match["parameters"] == [
            {"name": "options", "type": "array", "default": "array('force' => true)"},
        ]
        assert len(delete_instance_match["usage_examples"]) == 1
        delete_usage = delete_instance_match["usage_examples"][0]
        assert delete_usage["file"] == "mod/assign/externallib.php"
        assert delete_usage["usage_kind"] == "instance_method_call"
        assert delete_usage["confidence"] == "high"
        assert delete_usage["snippet"] == "return $assignment->delete_instance();"

        start_submission_definition = find_definition(connection, "mod_assign\\external\\start_submission::execute")
        assert start_submission_definition["total_matches"] == 1
        start_submission_match = start_submission_definition["matches"][0]
        assert start_submission_match["class_name"] == "mod_assign\\external\\start_submission"
        assert start_submission_match["visibility"] == "public"
        assert start_submission_match["is_static"] is True
        assert start_submission_match["signature"] == "public static function execute(int $assignmentid, bool $draft = false): array"
        assert start_submission_match["parameters"] == [
            {"name": "assignmentid", "type": "int", "default": None},
            {"name": "draft", "type": "bool", "default": "false"},
        ]
        assert start_submission_match["return_type"] == "array"
        assert start_submission_match["docblock_summary"] == "Start a submission attempt."
        assert start_submission_match["inheritance_role"] == "base_definition"
        assert start_submission_match["parent_class"] == "external_api"
        assert start_submission_match["usage_examples"][0] == {
            "file": "mod/assign/db/services.php",
            "line": 13,
            "usage_kind": "service_definition",
            "confidence": "high",
            "snippet": "mod_assign_start_submission",
        }
        assert {
            item["service_name"] for item in start_submission_match["linked_artifacts"]["services"]
        } == {"mod_assign_start_submission"}
        assert start_submission_match["linked_artifacts"]["services"][0]["implementation_file"] == (
            "mod/assign/classes/external/start_submission.php"
        )
        assert {
            item["file"] for item in start_submission_match["linked_artifacts"]["services"][0]["related_tests"]
        } == {"mod/assign/tests/external/start_submission_test.php"}
        assert any(example["usage_kind"] == "test_usage" for example in start_submission_match["usage_examples"][1:])
        assert start_submission_match["usage_summary"]["service_definition"] == 1
        assert start_submission_match["usage_summary"]["test_usage"] >= 1

        start_submission_with_leading_slash = find_definition(
            connection,
            "\\mod_assign\\external\\start_submission::execute",
        )
        assert start_submission_with_leading_slash["total_matches"] == 1
        assert start_submission_with_leading_slash["matches"][0]["fqname"] == start_submission_match["fqname"]

        start_submission_with_doubled_slashes = find_definition(
            connection,
            "mod_assign\\\\external\\\\start_submission::execute",
        )
        assert start_submission_with_doubled_slashes["total_matches"] == 1
        assert start_submission_with_doubled_slashes["matches"][0]["fqname"] == start_submission_match["fqname"]

        base_provider_definition = find_definition(connection, "core_ai\\provider::get_action_settings")
        assert base_provider_definition["total_matches"] == 1
        base_provider_match = base_provider_definition["matches"][0]
        assert base_provider_match["inheritance_role"] == "base_definition"
        assert base_provider_match["fqname"] == "core_ai\\provider::get_action_settings"

        openai_provider_definition = find_definition(connection, "aiprovider_openai\\provider::get_action_settings")
        assert openai_provider_definition["total_matches"] == 1
        openai_provider_match = openai_provider_definition["matches"][0]
        assert openai_provider_match["inheritance_role"] == "override"
        assert openai_provider_match["parent_class"] == "core_ai\\provider"
        assert openai_provider_match["overrides"] == "core_ai\\provider::get_action_settings"
        assert openai_provider_match["parent_definition"]["fqname"] == "core_ai\\provider::get_action_settings"
        assert openai_provider_match["overrides_definition"]["fqname"] == "core_ai\\provider::get_action_settings"
        assert openai_provider_match["parent_definition"]["fqname"] != "aiprovider_awsbedrock\\provider::get_action_settings"
        assert openai_provider_match["overrides_definition"]["fqname"] != "aiprovider_awsbedrock\\provider::get_action_settings"

        grading_app_definition = find_definition(connection, "mod_assign\\output\\grading_app")
        assert grading_app_definition["total_matches"] == 1
        grading_app_match = grading_app_definition["matches"][0]
        assert grading_app_match["symbol_type"] == "class"
        grading_artifacts = {item["path"] for item in grading_app_match["linked_artifacts"]["rendering"]}
        assert "mod/assign/classes/output/renderer.php" in grading_artifacts
        assert "mod/assign/templates/grading_app.mustache" in grading_artifacts

        action_form_definition = find_definition(connection, "aiprovider_openai\\form\\action_form")
        assert action_form_definition["total_matches"] == 1
        action_form_match = action_form_definition["matches"][0]
        form_artifacts = {item["path"] for item in action_form_match["linked_artifacts"]["rendering"]}
        assert "ai/classes/form/action_settings_form.php" in form_artifacts
        assert "lib/formslib.php" in form_artifacts

        ambiguous_execute = find_definition(connection, "execute", symbol_type="method")
        assert ambiguous_execute["total_matches"] >= 2
        assert {
            match["class_name"]
            for match in ambiguous_execute["matches"]
        } >= {
            "mod_assign\\external\\remove_submission",
            "mod_assign\\external\\start_submission",
        }

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
        assert "mod_assign_start_submission" in {
            item["service_name"] for item in assign_summary["linked_artifacts"]["service_navigation"]
        }
        assert {
            item["moodle_path"] for item in assign_summary["linked_artifacts"]["rendering_files"]
        } >= {
            "mod/assign/classes/output/renderer.php",
            "mod/assign/classes/output/grading_app.php",
            "mod/assign/templates/grading_app.mustache",
        }
        assert "core_ai/aiprovider_action_management_table" in {
            item["module_name"] for item in component_summary(connection, "core_ai")["js_modules"]
        }

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
        assert {
            item["implementation_file"] for item in services_context["linked_artifacts"]["services"]
        } >= {
            "mod/assign/externallib.php",
            "mod/assign/classes/external/start_submission.php",
        }
        service_chain = {
            item["service_name"]: item for item in services_context["linked_artifacts"]["services"]
        }
        assert {
            item["file"] for item in service_chain["mod_assign_start_submission"]["related_tests"]
        } == {"mod/assign/tests/external/start_submission_test.php"}

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
        assert "mod_assign_start_submission" in {
            item["service_name"] for item in assign_related["linked_artifacts"]["services"]
        }

        locallib_context = file_context(connection, "mod/assign/locallib.php")
        assert {
            item["class_name"] for item in locallib_context["rendering_references"]
        } == {
            "mod_assign\\output\\grading_app",
        }
        rendering_reference = locallib_context["rendering_references"][0]
        assert rendering_reference["resolved_target_file"] == "mod/assign/classes/output/grading_app.php"
        assert rendering_reference["template_files"] == ["mod/assign/templates/grading_app.mustache"]
        rendering_chain_paths = {
            item["path"] for item in locallib_context["linked_artifacts"]["rendering"]
        }
        assert "mod/assign/classes/output/grading_app.php" in rendering_chain_paths
        assert "mod/assign/templates/grading_app.mustache" in rendering_chain_paths
        assert "mod/assign/classes/output/renderer.php" in rendering_chain_paths

        locallib_related_paths = {item["path"] for item in locallib_context["related_suggestions"]}
        assert "mod/assign/classes/output/grading_app.php" in locallib_related_paths
        assert "mod/assign/templates/grading_app.mustache" in locallib_related_paths
        assert "mod/assign/classes/output/renderer.php" in locallib_related_paths

        locallib_related = suggest_related(connection, "mod/assign/locallib.php")
        locallib_suggestions = {item["path"]: item for item in locallib_related["suggestions"]}
        assert "mod/assign/classes/output/grading_app.php" in locallib_suggestions
        assert "\\mod_assign\\output\\grading_app" in locallib_suggestions[
            "mod/assign/classes/output/grading_app.php"
        ]["reason"]
        assert "mod/assign/templates/grading_app.mustache" in locallib_suggestions
        assert "Mustache template" in locallib_suggestions[
            "mod/assign/templates/grading_app.mustache"
        ]["reason"]
        assert "mod/assign/classes/output/renderer.php" in locallib_suggestions

        demo_locallib_context = file_context(connection, "mod/demo/locallib.php")
        demo_rendering_paths = {item["path"] for item in demo_locallib_context["linked_artifacts"]["rendering"]}
        assert "mod/demo/classes/output/widget.php" in demo_rendering_paths
        assert "mod/demo/classes/output/renderer.php" in demo_rendering_paths
        assert "mod/demo/templates/widget.mustache" in demo_rendering_paths
        assert "mod/demo/renderer.php" not in demo_rendering_paths

        demo_suggestions = {
            item["path"]: item for item in suggest_related(connection, "mod/demo/locallib.php")["suggestions"]
        }
        assert "mod/demo/classes/output/renderer.php" in demo_suggestions
        assert "mod/demo/renderer.php" not in demo_suggestions

        related_result = suggest_related(connection, "admin/tool/demo/settings.php")
        suggestions_by_path = {item["path"]: item for item in related_result["suggestions"]}
        assert suggestions_by_path["admin/tool/demo/lang/en/tool_demo.php"]["indexed"] is True

        mfa_context = file_context(connection, "admin/tool/mfa/settings.php")
        mfa_related = {item["path"]: item for item in mfa_context["related_suggestions"]}
        assert "lib/adminlib.php" in mfa_related
        assert "admin settings APIs" in mfa_related["lib/adminlib.php"]["reason"]

        mfa_suggestions = {item["path"]: item for item in suggest_related(connection, "admin/tool/mfa/settings.php")["suggestions"]}
        assert "lib/adminlib.php" in mfa_suggestions
        assert mfa_suggestions["lib/adminlib.php"]["indexed"] is True
        assert "admin settings APIs" in mfa_suggestions["lib/adminlib.php"]["reason"]

        provider_context = file_context(connection, "ai/provider/openai/classes/provider.php")
        provider_class_references = {
            item["class_name"]: item for item in provider_context["class_references"]
        }
        assert "aiprovider_openai\\form\\action_generate_image_form" in provider_class_references
        assert provider_class_references["aiprovider_openai\\form\\action_generate_image_form"][
            "resolved_target_file"
        ] == "ai/provider/openai/classes/form/action_generate_image_form.php"
        provider_related = {item["path"]: item for item in provider_context["related_suggestions"]}
        assert "ai/provider/openai/classes/form/action_generate_image_form.php" in provider_related
        assert "form\\action_generate_image_form" in provider_related[
            "ai/provider/openai/classes/form/action_generate_image_form.php"
        ]["reason"]

        provider_suggestions = {
            item["path"]: item for item in suggest_related(connection, "ai/provider/openai/classes/provider.php")["suggestions"]
        }
        assert "ai/provider/openai/classes/form/action_generate_image_form.php" in provider_suggestions
        assert "form\\action_generate_image_form" in provider_suggestions[
            "ai/provider/openai/classes/form/action_generate_image_form.php"
        ]["reason"]

        action_form_context = file_context(connection, "ai/provider/openai/classes/form/action_form.php")
        action_form_related = {item["path"]: item for item in action_form_context["related_suggestions"]}
        assert "ai/classes/form/action_settings_form.php" in action_form_related
        assert "extends action_settings_form" in action_form_related[
            "ai/classes/form/action_settings_form.php"
        ]["reason"]
        assert "lib/formslib.php" in action_form_related
        assert "inherits from moodleform" in action_form_related["lib/formslib.php"]["reason"]

        action_form_artifacts = {item["path"]: item for item in action_form_context["linked_artifacts"]["rendering"]}
        assert "ai/classes/form/action_settings_form.php" in action_form_artifacts
        assert "lib/formslib.php" in action_form_artifacts

        action_form_suggestions = {
            item["path"]: item
            for item in suggest_related(connection, "ai/provider/openai/classes/form/action_form.php")["suggestions"]
        }
        assert "ai/classes/form/action_settings_form.php" in action_form_suggestions
        assert action_form_suggestions["ai/classes/form/action_settings_form.php"]["indexed"] is True
        assert "lib/formslib.php" in action_form_suggestions
        assert action_form_suggestions["lib/formslib.php"]["indexed"] is True
        assert "inherits from moodleform" in action_form_suggestions["lib/formslib.php"]["reason"]

        js_context = file_context(connection, "ai/amd/src/aiprovider_action_management_table.js")
        assert js_context["js_module"]["module_name"] == "core_ai/aiprovider_action_management_table"
        assert js_context["js_module"]["export_kind"] == "default_class"
        assert js_context["js_module"]["superclass_module"] == "core_admin/plugin_management_table"
        assert js_context["js_module"]["resolved_superclass_file"] == "admin/amd/src/plugin_management_table.js"
        assert js_context["js_module"]["build_file"] == "ai/amd/build/aiprovider_action_management_table.min.js"
        js_imports = {(item["module_name"], item["resolved_target_file"]) for item in js_context["js_imports"]}
        assert ("core_admin/plugin_management_table", "admin/amd/src/plugin_management_table.js") in js_imports
        assert ("core/ajax", "lib/amd/src/ajax.js") in js_imports
        assert ("core_ai/local_actions", "ai/amd/src/local_actions.js") in js_imports
        js_import_map = {item["module_name"]: item for item in js_context["js_imports"]}
        assert js_import_map["core/ajax"]["imported_name"] == "call"
        assert js_import_map["core/ajax"]["local_name"] == "fetchMany"
        assert js_import_map["core/ajax"]["resolution_strategy"] == "indexed_registry"
        assert js_import_map["core_admin/plugin_management_table"]["resolution_strategy"] == "indexed_registry"
        js_related_paths = {item["path"] for item in js_context["related_suggestions"]}
        assert "admin/amd/src/plugin_management_table.js" in js_related_paths
        assert "lib/amd/src/ajax.js" in js_related_paths
        assert "ai/amd/src/local_actions.js" in js_related_paths
        assert "ai/amd/build/aiprovider_action_management_table.min.js" in js_related_paths

        js_related = suggest_related(connection, "ai/amd/src/aiprovider_action_management_table.js")
        js_suggestions = {item["path"]: item for item in js_related["suggestions"]}
        assert "admin/amd/src/plugin_management_table.js" in js_suggestions
        assert "imports core_admin/plugin_management_table" in js_suggestions[
            "admin/amd/src/plugin_management_table.js"
        ]["reason"]
        assert "lib/amd/src/ajax.js" in js_suggestions
        assert "imports core/ajax" in js_suggestions["lib/amd/src/ajax.js"]["reason"]
        assert "ai/amd/src/local_actions.js" in js_suggestions
        assert "imports core_ai/local_actions" in js_suggestions["ai/amd/src/local_actions.js"]["reason"]
        assert "ai/amd/build/aiprovider_action_management_table.min.js" in js_suggestions
        assert "built artifact" in js_suggestions["ai/amd/build/aiprovider_action_management_table.min.js"]["reason"]
        assert js_related["linked_artifacts"]["javascript"]["build_artifact"]["path"] == (
            "ai/amd/build/aiprovider_action_management_table.min.js"
        )
        assert {
            item["file"] for item in js_related["linked_artifacts"]["javascript"]["imports"] if item["file"]
        } >= {
            "admin/amd/src/plugin_management_table.js",
            "lib/amd/src/ajax.js",
            "ai/amd/src/local_actions.js",
        }

        legacy_js_context = file_context(connection, "mod/forum/amd/src/forum.js")
        assert legacy_js_context["js_module"]["module_name"] == "mod_forum/forum"
        assert legacy_js_context["js_module"]["export_kind"] == "amd_return_object"
        legacy_imports = {item["module_name"]: item for item in legacy_js_context["js_imports"]}
        assert legacy_imports["jquery"]["resolved_target_file"] is None
        assert legacy_imports["jquery"]["resolution_status"] == "external"
        assert legacy_imports["jquery"]["resolution_strategy"] == "external_runtime"
        assert legacy_imports["jquery"]["is_external"] is True
        assert legacy_imports["core/ajax"]["resolved_target_file"] == "lib/amd/src/ajax.js"
        assert legacy_imports["core/ajax"]["resolution_strategy"] == "indexed_registry"
        assert legacy_imports["mod_forum/repository"]["resolved_target_file"] == "mod/forum/amd/src/repository.js"
        assert legacy_imports["mod_forum/repository"]["resolution_strategy"] == "indexed_registry"
        legacy_related_paths = {item["path"] for item in legacy_js_context["related_suggestions"]}
        assert "lib/amd/src/ajax.js" in legacy_related_paths
        assert "mod/forum/amd/src/repository.js" in legacy_related_paths
        assert "mod/forum/amd/build/forum.min.js" in legacy_related_paths

        legacy_related = suggest_related(connection, "mod/forum/amd/src/forum.js")
        legacy_suggestions = {item["path"]: item for item in legacy_related["suggestions"]}
        assert "lib/amd/src/ajax.js" in legacy_suggestions
        assert "imports core/ajax" in legacy_suggestions["lib/amd/src/ajax.js"]["reason"]
        assert "mod/forum/amd/src/repository.js" in legacy_suggestions
        assert "imports mod_forum/repository" in legacy_suggestions["mod/forum/amd/src/repository.js"]["reason"]
        assert "mod/forum/amd/build/forum.min.js" in legacy_suggestions
        assert "built artifact" in legacy_suggestions["mod/forum/amd/build/forum.min.js"]["reason"]

        js_definition = find_definition(connection, "core/ajax", symbol_type="js_module")
        assert js_definition["total_matches"] == 1
        js_definition_match = js_definition["matches"][0]
        assert js_definition_match["symbol_type"] == "js_module"
        assert js_definition_match["module_name"] == "core/ajax"
        assert js_definition_match["file"] == "lib/amd/src/ajax.js"
        assert js_definition_match["build_file"] == "lib/amd/build/ajax.min.js"
        assert js_definition_match["linked_artifacts"]["javascript"]["build_artifact"]["path"] == "lib/amd/build/ajax.min.js"
        assert any(
            item["module_name"] == "core_ai/aiprovider_action_management_table"
            for item in js_definition_match["linked_artifacts"]["javascript"]["imported_by"]
        )
        assert {
            item["file"] for item in js_definition_match["usage_examples"]
        } >= {
            "ai/amd/src/aiprovider_action_management_table.js",
            "mod/forum/amd/src/forum.js",
        }
        assert js_definition_match["usage_summary"]["js_import_usage"] >= 2

        ai_js_definition = find_definition(connection, "core_ai/aiprovider_action_management_table")
        assert ai_js_definition["total_matches"] == 1
        ai_js_match = ai_js_definition["matches"][0]
        assert ai_js_match["module_name"] == "core_ai/aiprovider_action_management_table"
        assert ai_js_match["resolved_superclass_file"] == "admin/amd/src/plugin_management_table.js"
        assert ai_js_match["linked_artifacts"]["javascript"]["build_artifact"]["path"] == (
            "ai/amd/build/aiprovider_action_management_table.min.js"
        )
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
        if file_path.as_posix().endswith("mod/forum/renderer.php"):
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

"""End-to-end and extractor-focused tests for the Phase 1 indexer."""

from __future__ import annotations

from pathlib import Path

from moodle_indexer.config import build_index_config
from moodle_indexer.extractors import (
    extract_capabilities,
    extract_language_strings,
    extract_php_artifacts,
)
from moodle_indexer.indexer import build_index
from moodle_indexer.queries import component_summary, file_context, find_symbol, suggest_related
from moodle_indexer.store import open_database


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "moodle_sample"


def _read_fixture(relative_path: str) -> str:
    """Read one source fixture from the synthetic Moodle tree."""

    return (FIXTURE_ROOT / relative_path).read_text(encoding="utf-8")


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


def test_build_index_and_query_endpoints(tmp_path: Path) -> None:
    db_path = tmp_path / "moodle-index.sqlite"
    result = build_index(build_index_config(str(FIXTURE_ROOT), str(db_path)))

    assert result["files"] >= 10
    assert result["components"] >= 2
    assert result["symbols"] >= 12
    assert result["relationships"] >= 6
    assert result["capabilities"] == 1
    assert result["language_strings"] >= 4
    assert result["tests"] >= 4

    connection = open_database(db_path)
    try:
        symbol_result = find_symbol(connection, "discussion_exporter")
        assert symbol_result["matches"]
        discussion_exporter = symbol_result["matches"][0]
        assert discussion_exporter["component"] == "mod_forum"
        assert discussion_exporter["file_role"] == "external_api_class"
        assert any(
            item["type"] == "extends" and item["target"] == "\\external_api"
            for item in discussion_exporter["relationships"]
        )

        method_result = find_symbol(connection, "export_for_template")
        assert method_result["matches"][0]["container_name"] == "mod_forum\\output\\discussion_list"
        assert any(
            item["type"] == "method_of"
            and item["target"] == "mod_forum\\output\\discussion_list"
            for item in method_result["matches"][0]["relationships"]
        )

        access_context = file_context(connection, FIXTURE_ROOT, "mod/forum/db/access.php")
        assert access_context["component"] == "mod_forum"
        assert access_context["file_role"] == "access_definition"
        assert access_context["capabilities"][0]["name"] == "mod/forum:viewdiscussion"
        assert access_context["capabilities"][0]["archetypes"]["student"] == "CAP_ALLOW"
        assert any(
            suggestion["path"] == "mod/forum/lang/en/mod_forum.php"
            and suggestion["reason"]
            for suggestion in access_context["related_suggestions"]
        )

        renderer_context = file_context(connection, FIXTURE_ROOT, "mod/forum/renderer.php")
        assert renderer_context["string_usages"] == [
            {"component_name": "mod_forum", "line": 8, "string_key": "pluginname"}
        ]
        assert any(
            relationship["relationship_type"] == "extends"
            for relationship in renderer_context["relationships"]
        )

        lib_context = file_context(connection, FIXTURE_ROOT, "mod/forum/lib.php")
        assert lib_context["capability_checks"] == [
            {
                "capability_name": "mod/forum:viewdiscussion",
                "function_name": "require_capability",
                "line": 7,
            }
        ]

        component_result = component_summary(connection, "mod_forum")
        assert component_result["stats"]["language_string_count"] == 2
        assert component_result["stats"]["capability_check_count"] == 1
        assert component_result["stats"]["relationship_count"] >= 6
        assert component_result["key_file_roles"]["access_definition"] == 1
        assert any(
            symbol["symbol_type"] == "method"
            and symbol["fqname"] == "mod_forum\\output\\discussion_list::export_for_template"
            for symbol in component_result["sample_symbols"]
        )

        related_result = suggest_related(connection, FIXTURE_ROOT, "admin/tool/demo/settings.php")
        suggestions_by_path = {item["path"]: item for item in related_result["suggestions"]}
        assert suggestions_by_path["admin/tool/demo/lang/en/tool_demo.php"]["indexed"] is True
        assert "language strings" in suggestions_by_path["admin/tool/demo/lang/en/tool_demo.php"]["reason"]
        assert suggestions_by_path["admin/tool/demo/version.php"]["indexed"] is False
    finally:
        connection.close()

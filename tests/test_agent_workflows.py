# Copyright (c) Moodle Pty Ltd. All rights reserved.
# Licensed under the Moodle Community License v1.3.
# See LICENSE.md in the repository root for full terms.
# Commercial use requires a separate written agreement with Moodle.

"""Targeted coverage for planning, semantic, and safety helper workflows."""

from __future__ import annotations

from pathlib import Path

from moodle_indexer.queries import (
    _classify_change_risk,
    _collect_contract_checks,
    _collect_post_edit_checks,
    _collect_pre_edit_checks,
    _plan_profile_for_query,
    _representative_service_pattern,
    assess_test_impact,
    build_context_bundle,
    execution_guardrails,
    propose_change_plan,
    semantic_context,
)


FREE_TEXT_EXTERNAL_API_QUERY = "add a parameter to a Moodle external API method and update its tests"


def test_representative_service_pattern_selects_canonical_mod_assign_example(classic_connection) -> None:
    pattern = _representative_service_pattern(classic_connection)

    assert pattern is not None
    assert pattern["implementation_path"] == "mod/assign/classes/external/remove_submission.php"
    assert pattern["implementation_symbol"] == "mod_assign\\external\\remove_submission::execute"
    assert pattern["service_path"] == "mod/assign/db/services.php"
    assert pattern["test_path"] == "mod/assign/tests/external/remove_submission_test.php"


def test_free_text_safety_helpers_materialize_canonical_service_pattern(classic_connection) -> None:
    profile = _plan_profile_for_query(FREE_TEXT_EXTERNAL_API_QUERY)
    profile["representative_pattern"] = _representative_service_pattern(classic_connection)
    plan = propose_change_plan(classic_connection, query_text=FREE_TEXT_EXTERNAL_API_QUERY)
    test_impact = assess_test_impact(classic_connection, query_text=FREE_TEXT_EXTERNAL_API_QUERY)

    contract_checks = _collect_contract_checks(profile, plan, limit=4)
    pre_edit_checks = _collect_pre_edit_checks(profile, plan, test_impact, limit=5)
    post_edit_checks = _collect_post_edit_checks(profile, plan, test_impact, limit=5)

    contract_paths = [item.get("path") for item in contract_checks]
    pre_paths = [item.get("path") for item in pre_edit_checks]
    post_paths = [item.get("path") for item in post_edit_checks]

    assert contract_paths[:3] == [
        None,
        "mod/assign/classes/external/remove_submission.php",
        "mod/assign/db/services.php",
    ]
    assert any("signature changes" in item["reason"].lower() for item in contract_checks)
    assert pre_paths[0] is None
    assert pre_paths[1] == "mod/assign/classes/external/remove_submission.php"
    assert "mod/assign/db/services.php" in pre_paths
    assert "mod/assign/tests/external/remove_submission_test.php" in pre_paths
    assert post_paths[:2] == [
        "mod/assign/db/services.php",
        "mod/assign/tests/external/remove_submission_test.php",
    ]
    assert "mod/assign/tests/external/start_submission_test.php" in post_paths


def test_free_text_change_risk_stays_high_for_unanchored_service_contract_changes(classic_connection) -> None:
    profile = _plan_profile_for_query(FREE_TEXT_EXTERNAL_API_QUERY)
    profile["representative_pattern"] = _representative_service_pattern(classic_connection)
    plan = propose_change_plan(classic_connection, query_text=FREE_TEXT_EXTERNAL_API_QUERY)
    test_impact = assess_test_impact(classic_connection, query_text=FREE_TEXT_EXTERNAL_API_QUERY)

    risk = _classify_change_risk(profile, plan, test_impact)

    assert risk["level"] == "high"
    assert "external api contract change" in risk["reason"].lower()


def test_semantic_context_free_text_prefers_external_api_examples_with_tests(classic_connection) -> None:
    semantic = semantic_context(
        classic_connection,
        query_text="examples of Moodle external API methods with PHPUnit coverage",
    )

    primary_paths = [item["path"] for item in semantic["primary_semantic_context"]]
    secondary_paths = [item["path"] for item in semantic["secondary_semantic_context"]]

    assert any("/classes/external/" in path or path.endswith("/externallib.php") for path in primary_paths)
    assert any("/tests/external/" in path or path.endswith("_test.php") for path in primary_paths[:2] + secondary_paths[:4])
    assert len(primary_paths) <= 5
    assert not (set(primary_paths) & set(secondary_paths))


def test_execution_guardrails_free_text_materializes_service_pattern(classic_connection) -> None:
    payload = execution_guardrails(classic_connection, query_text=FREE_TEXT_EXTERNAL_API_QUERY)

    pre_paths = [item.get("path") for item in payload["pre_edit_checks"]]
    post_paths = [item.get("path") for item in payload["post_edit_checks"]]

    assert payload["change_risk"]["level"] == "high"
    assert pre_paths[0] is None
    assert pre_paths[1] == "mod/assign/classes/external/remove_submission.php"
    assert "mod/assign/db/services.php" in pre_paths
    assert "mod/assign/tests/external/remove_submission_test.php" in pre_paths
    assert post_paths[:2] == [
        "mod/assign/db/services.php",
        "mod/assign/tests/external/remove_submission_test.php",
    ]
    assert "mod/assign/tests/external/start_submission_test.php" in post_paths
    assert any("canonical moodle service pattern" in item["reason"].lower() for item in payload["pre_edit_checks"])


def test_build_context_bundle_service_slice_packages_core_working_set(classic_connection) -> None:
    bundle = build_context_bundle(
        classic_connection,
        symbol_query="mod_assign\\external\\start_submission::execute",
    )

    primary_paths = [item["path"] for item in bundle["primary_context"]]
    test_paths = [item["path"] for item in bundle["tests_to_consider"]]

    assert bundle["anchor"]["file"] == "mod/assign/classes/external/start_submission.php"
    assert primary_paths[:3] == [
        "mod/assign/classes/external/start_submission.php",
        "mod/assign/db/services.php",
        "mod/assign/tests/external/start_submission_test.php",
    ]
    assert "mod/assign/tests/external/start_submission_test.php" in test_paths
    assert bundle["guardrails"]["change_risk"]["level"] == "medium"
    assert bundle["recommended_reading_order"][0]["target"] == "mod/assign/classes/external/start_submission.php"
    assert bundle["bundle_stats"]["primary_count"] <= 4


def test_build_context_bundle_rendering_slice_stays_local_and_high_risk(classic_connection) -> None:
    bundle = build_context_bundle(classic_connection, symbol_query="assign::view")

    supporting_paths = [item["path"] for item in bundle["supporting_context"]]

    assert bundle["guardrails"]["change_risk"]["level"] == "high"
    assert bundle["recommended_reading_order"][0]["target"] == "mod/assign/locallib.php"
    assert supporting_paths[:3] == [
        "mod/assign/classes/output/grading_app.php",
        "mod/assign/classes/output/renderer.php",
        "mod/assign/templates/grading_app.mustache",
    ]
    assert "mod/assign/classes/local/assign_base.php" not in supporting_paths[:3]
    assert bundle["bundle_stats"]["supporting_count"] <= 5


def test_build_context_bundle_rendering_file_bundle_prioritizes_local_render_chain(classic_connection) -> None:
    bundle = build_context_bundle(classic_connection, file_path="mod/assign/locallib.php")

    supporting_paths = [item["path"] for item in bundle["supporting_context"]]

    assert supporting_paths[:3] == [
        "mod/assign/classes/output/grading_app.php",
        "mod/assign/classes/output/renderer.php",
        "mod/assign/templates/grading_app.mustache",
    ]
    assert supporting_paths.index("mod/assign/db/services.php") > supporting_paths.index("mod/assign/templates/grading_app.mustache")


def test_build_context_bundle_output_class_stays_same_component_and_honest(classic_connection) -> None:
    bundle = build_context_bundle(classic_connection, symbol_query="mod_assign\\output\\grading_app")

    supporting_paths = [item["path"] for item in bundle["supporting_context"]]
    optional_paths = [item["path"] for item in bundle["optional_context"]]

    assert supporting_paths[:2] == [
        "mod/assign/classes/output/renderer.php",
        "mod/assign/templates/grading_app.mustache",
    ]
    assert "mod/demo/classes/output/renderer.php" not in supporting_paths
    assert "mod/demo/classes/output/renderer.php" in optional_paths


def test_build_context_bundle_provider_form_slice_packages_form_chain(classic_connection) -> None:
    bundle = build_context_bundle(
        classic_connection,
        symbol_query="aiprovider_openai\\provider::get_action_settings",
    )

    primary_paths = [item["path"] for item in bundle["primary_context"]]
    supporting_paths = [item["path"] for item in bundle["supporting_context"]]

    assert primary_paths[:2] == [
        "ai/provider/openai/classes/provider.php",
        "ai/provider/openai/classes/form/action_generate_image_form.php",
    ]
    assert supporting_paths[:3] == [
        "ai/classes/form/action_settings_form.php",
        "ai/provider/openai/classes/form/action_form.php",
        "ai/classes/provider.php",
    ]
    assert "lib/formslib.php" in supporting_paths
    assert bundle["guardrails"]["change_risk"]["level"] == "medium"


def test_build_context_bundle_js_slice_packages_source_neighbors_and_build_artifact(classic_connection) -> None:
    bundle = build_context_bundle(
        classic_connection,
        symbol_query="core_ai/aiprovider_action_management_table",
    )

    primary_paths = [item["path"] for item in bundle["primary_context"]]
    supporting_paths = [item["path"] for item in bundle["supporting_context"]]
    optional_paths = [item["path"] for item in bundle["optional_context"]]

    assert primary_paths == ["ai/amd/src/aiprovider_action_management_table.js"]
    assert supporting_paths[:3] == [
        "admin/amd/src/plugin_management_table.js",
        "ai/amd/src/local_actions.js",
        "lib/amd/src/ajax.js",
    ]
    assert "ai/amd/build/aiprovider_action_management_table.min.js" in optional_paths
    assert bundle["guardrails"]["change_risk"]["level"] == "medium"


def test_build_context_bundle_free_text_materializes_canonical_service_pattern(classic_connection) -> None:
    bundle = build_context_bundle(classic_connection, query_text=FREE_TEXT_EXTERNAL_API_QUERY)

    primary_paths = [item["path"] for item in bundle["primary_context"]]
    example_paths = [item["path"] for item in bundle["example_patterns"]]
    test_paths = [item["path"] for item in bundle["tests_to_consider"]]

    assert primary_paths[:3] == [
        "mod/assign/classes/external/remove_submission.php",
        "mod/assign/db/services.php",
        "mod/assign/tests/external/remove_submission_test.php",
    ]
    assert "mod/assign/tests/external/remove_submission_test.php" in test_paths
    assert example_paths[:3] == [
        "mod/assign/classes/external/remove_submission.php",
        "mod/assign/db/services.php",
        "mod/assign/tests/external/remove_submission_test.php",
    ]
    assert any(
        "signature" in item["reason"].lower()
        for item in bundle["guardrails"]["pre_edit_checks"] + bundle["guardrails"]["post_edit_checks"]
    )
    assert bundle["bundle_stats"]["primary_count"] <= 4


def test_semantic_context_free_text_boost_login_query_prefers_exact_local_files(classic_connection) -> None:
    semantic = semantic_context(
        classic_connection,
        query_text="Moodle login form helper icons for username/password fields in Boost theme. Find relevant template, SCSS, and nearby test pattern for asserting rendered icons or login form UI.",
    )

    primary_paths = [item["path"] for item in semantic["primary_semantic_context"]]
    secondary_paths = [item["path"] for item in semantic["secondary_semantic_context"]]
    combined = primary_paths + secondary_paths

    assert "theme/boost/templates/core/loginform.mustache" in combined
    assert "theme/boost/scss/moodle/login.scss" in combined
    assert "login/tests/behat/login_render.feature" in combined


def test_build_context_bundle_free_text_boost_login_query_surfaces_exact_files(classic_connection) -> None:
    bundle = build_context_bundle(
        classic_connection,
        query_text="For MDL-88194, find the exact Moodle files that control the Boost primary login form UI, the SCSS that styles it, and the nearest existing Behat tests for login-page rendering.",
    )

    primary_paths = [item["path"] for item in bundle["primary_context"]]
    supporting_paths = [item["path"] for item in bundle["supporting_context"]]
    test_paths = [item["path"] for item in bundle["tests_to_consider"]]
    combined = primary_paths + supporting_paths

    assert "theme/boost/templates/core/loginform.mustache" in combined
    assert "theme/boost/scss/moodle/login.scss" in combined
    assert "login/tests/behat/login_render.feature" in test_paths


def test_build_context_bundle_free_text_tiny_premium_query_surfaces_exact_wiring_files(classic_connection) -> None:
    bundle = build_context_bundle(
        classic_connection,
        query_text="MDL-88547 tiny_premium markdown plugin support existing Moodle patterns capability setting tests docs",
    )

    primary_paths = [item["path"] for item in bundle["primary_context"]]
    supporting_paths = [item["path"] for item in bundle["supporting_context"]]
    optional_paths = [item["path"] for item in bundle["optional_context"]]
    test_paths = [item["path"] for item in bundle["tests_to_consider"]]
    combined = primary_paths + supporting_paths + optional_paths

    assert "editor/tiny/plugins/premium/db/access.php" in combined
    assert "editor/tiny/plugins/premium/lang/en/tiny_premium.php" in combined
    assert "editor/tiny/plugins/premium/amd/src/configuration.js" in combined
    assert "editor/tiny/plugins/premium/version.php" in combined
    assert "editor/tiny/plugins/premium/tests/manager_test.php" in test_paths or "editor/tiny/plugins/premium/tests/behat/markdown.feature" in test_paths


def test_semantic_context_explicit_theme_boost_anchor_keeps_top_hits_in_subtree(classic_connection) -> None:
    semantic = semantic_context(
        classic_connection,
        query_text="theme/boost login tests mustache scss behat",
    )

    primary_paths = [item["path"] for item in semantic["primary_semantic_context"]]

    assert primary_paths[:2] == [
        "theme/boost/scss/moodle/login.scss",
        "theme/boost/templates/core/loginform.mustache",
    ]
    assert not any(path.startswith("mod/assign/tests/") for path in primary_paths[:3])


def test_semantic_context_explicit_lib_editor_tiny_premium_anchor_stays_local(classic_connection) -> None:
    semantic = semantic_context(
        classic_connection,
        query_text="lib/editor/tiny/plugins/premium tests configuration",
    )

    primary_paths = [item["path"] for item in semantic["primary_semantic_context"]]
    secondary_paths = [item["path"] for item in semantic["secondary_semantic_context"]]

    assert primary_paths
    assert all(path.startswith("editor/tiny/plugins/premium/") for path in primary_paths)
    assert secondary_paths[0].startswith("editor/tiny/plugins/premium/")

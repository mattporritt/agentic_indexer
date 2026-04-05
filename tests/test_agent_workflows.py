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

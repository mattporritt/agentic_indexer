"""Focused tests for the stable runtime-facing JSON contract wrapper."""

from __future__ import annotations

import json
from pathlib import Path

from moodle_indexer.cli import main
from moodle_indexer.runtime_contract import runtime_contract_schema


def _assert_runtime_envelope(payload: dict) -> None:
    schema = runtime_contract_schema()
    assert set(schema["required_top_level_fields"]).issubset(payload.keys())
    assert payload["tool"] == "agentic_indexer"
    assert payload["version"] == "v1"
    assert isinstance(payload["query"], str)
    assert isinstance(payload["normalized_query"], str)
    assert isinstance(payload["intent"], dict)
    assert isinstance(payload["results"], list)


def _assert_runtime_result(result: dict) -> None:
    schema = runtime_contract_schema()
    assert set(schema["required_result_fields"]).issubset(result.keys())
    assert isinstance(result["id"], str)
    assert isinstance(result["type"], str)
    assert isinstance(result["rank"], int)
    assert result["confidence"] in {"high", "medium", "low"}
    assert isinstance(result["content"], dict)
    assert isinstance(result["diagnostics"], dict)
    assert isinstance(result["source"], dict)
    assert set(schema["required_source_fields"]).issubset(result["source"].keys())
    assert result["source"]["heading_path"] == list(result["source"]["heading_path"])


def test_find_definition_json_contract_has_stable_outer_envelope(classic_db_path: Path, capsys) -> None:
    exit_code = main(
        [
            "find-definition",
            "--db-path",
            str(classic_db_path),
            "--symbol",
            "mod_assign\\external\\start_submission::execute",
            "--json-contract",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    _assert_runtime_envelope(payload)
    assert payload["query"] == "mod_assign\\external\\start_submission::execute"
    assert payload["normalized_query"] == "mod_assign\\external\\start_submission::execute"
    assert payload["intent"] == {
        "command": "find-definition",
        "query_kind": "symbol",
        "response_mode": "definition_lookup",
        "symbol_type_filter": "any",
        "include_usages": True,
        "limit": 10,
    }
    result = payload["results"][0]
    _assert_runtime_result(result)
    assert result["type"] == "definition_match"
    assert result["rank"] == 1
    assert result["confidence"] == "high"
    assert result["source"] == {
        "name": "code_index",
        "type": "indexed_codebase",
        "url": None,
        "canonical_url": None,
        "path": "mod/assign/classes/external/start_submission.php",
        "document_title": None,
        "section_title": None,
        "heading_path": [],
    }
    assert isinstance(result["content"]["implements_definitions"], list)
    assert isinstance(result["content"]["usage_examples"], list)
    assert result["content"]["parent_definition"] is None
    assert result["diagnostics"]["selection_strategy"] == "definition_lookup"


def test_semantic_context_json_contract_has_consistent_provenance_and_lists(classic_db_path: Path, capsys) -> None:
    exit_code = main(
        [
            "semantic-context",
            "--db-path",
            str(classic_db_path),
            "--symbol",
            "mod_assign\\external\\start_submission::execute",
            "--json-contract",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    _assert_runtime_envelope(payload)
    assert payload["intent"]["command"] == "semantic-context"
    assert payload["intent"]["query_kind"] == "symbol"
    assert payload["intent"]["response_mode"] == "semantic_context"
    first = payload["results"][0]
    _assert_runtime_result(first)
    assert first["type"] == "semantic_context"
    assert first["source"]["name"] == "code_index"
    assert first["source"]["type"] == "indexed_codebase"
    assert first["source"]["url"] is None
    assert first["source"]["canonical_url"] is None
    assert first["source"]["heading_path"] == []
    assert isinstance(first["diagnostics"]["retrieval_sources"], list)
    assert first["content"]["chunk_id"]
    assert first["content"]["why_relevant_to_anchor"]


def test_build_context_bundle_json_contract_packages_bundle_as_single_result(classic_db_path: Path, capsys) -> None:
    argv = [
        "build-context-bundle",
        "--db-path",
        str(classic_db_path),
        "--query",
        "add a parameter to a Moodle external API method and update its tests",
        "--json-contract",
    ]

    first_exit = main(argv)
    assert first_exit == 0
    first_payload = json.loads(capsys.readouterr().out)

    second_exit = main(argv)
    assert second_exit == 0
    second_payload = json.loads(capsys.readouterr().out)

    assert first_payload == second_payload
    _assert_runtime_envelope(first_payload)
    assert first_payload["intent"]["command"] == "build-context-bundle"
    assert first_payload["intent"]["query_kind"] == "query"
    assert first_payload["results"][0]["id"] == second_payload["results"][0]["id"]
    result = first_payload["results"][0]
    _assert_runtime_result(result)
    assert result["type"] == "context_bundle"
    assert result["confidence"] == "medium"
    assert result["source"]["path"] == "mod/assign/classes/external/remove_submission.php"
    assert isinstance(result["content"]["primary_context"], list)
    assert isinstance(result["content"]["supporting_context"], list)
    assert isinstance(result["content"]["optional_context"], list)
    assert isinstance(result["content"]["tests_to_consider"], list)
    assert isinstance(result["content"]["example_patterns"], list)
    assert isinstance(result["content"]["recommended_reading_order"], list)
    assert isinstance(result["content"]["recommended_next_actions"], list)
    assert isinstance(result["content"]["guardrails"]["pre_edit_checks"], list)
    assert result["content"]["primary_context"][0]["id"]
    assert result["content"]["guardrails"]["pre_edit_checks"][0]["id"]
    assert result["diagnostics"]["selection_strategy"] == "context_bundle"


def test_find_definition_json_contract_empty_results_stays_stable(classic_db_path: Path, capsys) -> None:
    exit_code = main(
        [
            "find-definition",
            "--db-path",
            str(classic_db_path),
            "--symbol",
            "nonexistent_symbol_for_contract_test",
            "--json-contract",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    _assert_runtime_envelope(payload)
    assert payload["results"] == []
    assert payload["intent"]["command"] == "find-definition"
    assert payload["intent"]["symbol_type_filter"] == "any"


def test_semantic_context_json_contract_error_path_keeps_full_envelope(classic_db_path: Path, capsys) -> None:
    exit_code = main(
        [
            "semantic-context",
            "--db-path",
            str(classic_db_path),
            "--file",
            "missing/path/not_in_index.php",
            "--json-contract",
        ]
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    _assert_runtime_envelope(payload)
    assert payload["query"] == "missing/path/not_in_index.php"
    assert payload["normalized_query"] == "missing/path/not_in_index.php"
    assert payload["intent"]["command"] == "semantic-context"
    assert payload["results"] == []

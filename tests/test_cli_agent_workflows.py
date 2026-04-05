"""CLI integration coverage for newer agent-oriented navigation endpoints."""

from __future__ import annotations

import json
from pathlib import Path

from moodle_indexer.cli import main


def test_cli_agent_endpoints_cover_service_js_and_free_text_workflows(
    classic_db_path: Path,
    capsys,
) -> None:
    commands = [
        (
            [
                "semantic-context",
                "--db-path",
                str(classic_db_path),
                "--symbol",
                "mod_assign\\external\\start_submission::execute",
            ],
            lambda payload: (
                payload["data"]["primary_semantic_context"][0]["path"]
                == "mod/assign/classes/external/start_submission.php"
                and any(
                    item["path"] == "mod/assign/tests/external/start_submission_test.php"
                    for item in payload["data"]["primary_semantic_context"]
                )
            ),
        ),
        (
            [
                "propose-change-plan",
                "--db-path",
                str(classic_db_path),
                "--symbol",
                "core_ai/aiprovider_action_management_table",
            ],
            lambda payload: (
                payload["data"]["required_edits"][0]["path"]
                == "ai/amd/src/aiprovider_action_management_table.js"
                and any(
                    item["path"] == "ai/amd/build/aiprovider_action_management_table.min.js"
                    for item in payload["data"]["optional_edits"]
                )
            ),
        ),
        (
            [
                "assess-test-impact",
                "--db-path",
                str(classic_db_path),
                "--query",
                "add a parameter to a Moodle external API method and update its tests",
            ],
            lambda payload: (
                any(
                    item.get("path") == "mod/assign/tests/external/remove_submission_test.php"
                    for item in payload["data"]["direct_tests"]
                )
                and any(
                    item.get("path") == "mod/assign/db/services.php"
                    for item in payload["data"]["contract_checks"]
                )
            ),
        ),
        (
            [
                "execution-guardrails",
                "--db-path",
                str(classic_db_path),
                "--query",
                "add a parameter to a Moodle external API method and update its tests",
            ],
            lambda payload: (
                payload["data"]["change_risk"]["level"] == "high"
                and any(
                    item.get("path") == "mod/assign/classes/external/remove_submission.php"
                    for item in payload["data"]["pre_edit_checks"]
                )
                and any(
                    item.get("path") == "mod/assign/tests/external/remove_submission_test.php"
                    for item in payload["data"]["post_edit_checks"]
                )
            ),
        ),
        (
            [
                "build-context-bundle",
                "--db-path",
                str(classic_db_path),
                "--query",
                "add a parameter to a Moodle external API method and update its tests",
            ],
            lambda payload: (
                payload["data"]["primary_context"][0]["path"] == "mod/assign/classes/external/remove_submission.php"
                and payload["data"]["primary_context"][1]["path"] == "mod/assign/db/services.php"
                and any(
                    item["path"] == "mod/assign/tests/external/remove_submission_test.php"
                    for item in payload["data"]["tests_to_consider"]
                )
                and payload["data"]["bundle_stats"]["primary_count"] <= 4
            ),
        ),
    ]

    for argv, predicate in commands:
        exit_code = main(argv)
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert predicate(payload), argv

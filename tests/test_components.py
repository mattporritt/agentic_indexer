# Copyright (c) Moodle Pty Ltd. All rights reserved.
# Licensed under the Moodle Community License v1.3.
# See LICENSE.md in the repository root for full terms.
# Commercial use requires a separate written agreement with Moodle.

"""Tests for Moodle component inference and file-role rules."""

from __future__ import annotations

import pytest

from moodle_indexer.components import infer_component
from moodle_indexer.file_roles import classify_file_role
from moodle_indexer.subplugins import SubpluginMount


@pytest.mark.parametrize(
    ("path", "expected_name", "expected_type", "expected_root"),
    [
        ("mod/forum/lib.php", "mod_forum", "mod", "mod/forum"),
        ("blocks/rss_client/block_rss_client.php", "blocks_rss_client", "blocks", "blocks/rss_client"),
        ("local/demo/settings.php", "local_demo", "local", "local/demo"),
        ("admin/tool/demo/settings.php", "tool_demo", "tool", "admin/tool/demo"),
        ("admin/report/log/index.php", "report_log", "report", "admin/report/log"),
        ("enrol/manual/lib.php", "enrol_manual", "enrol", "enrol/manual"),
        ("auth/oauth2/classes/api.php", "auth_oauth2", "auth", "auth/oauth2"),
        ("repository/filesystem/lib.php", "repository_filesystem", "repository", "repository/filesystem"),
        ("question/type/multichoice/questiontype.php", "qtype_multichoice", "qtype", "question/type/multichoice"),
        ("question/behaviour/deferredfeedback/behaviour.php", "qbehaviour_deferredfeedback", "qbehaviour", "question/behaviour/deferredfeedback"),
        ("question/format/xhtml/format.php", "qformat_xhtml", "qformat", "question/format/xhtml"),
        ("availability/condition/date/classes/condition.php", "availability_date", "availability", "availability/condition/date"),
        ("course/format/topics/lib.php", "format_topics", "format", "course/format/topics"),
        ("grade/report/grader/lib.php", "gradereport_grader", "gradereport", "grade/report/grader"),
        ("grade/export/xls/lib.php", "gradeexport_xls", "gradeexport", "grade/export/xls"),
        ("grade/import/csv/lib.php", "gradeimport_csv", "gradeimport", "grade/import/csv"),
        ("editor/tiny/classes/plugin.php", "editor_tiny", "editor", "editor/tiny"),
        ("media/player/videojs/lib.php", "media_videojs", "media", "media/player/videojs"),
        ("plagiarism/turnitin/lib.php", "plagiarism_turnitin", "plagiarism", "plagiarism/turnitin"),
        ("theme/boost/lib.php", "theme_boost", "theme", "theme/boost"),
        ("payment/gateway/paypal/classes/gateway.php", "paygw_paypal", "paygw", "payment/gateway/paypal"),
        ("contentbank/contenttype/h5p/lib.php", "contenttype_h5p", "contenttype", "contentbank/contenttype/h5p"),
        ("ai/provider/openai/classes/provider.php", "aiprovider_openai", "aiprovider", "ai/provider/openai"),
        ("ai/amd/src/aiprovider_action_management_table.js", "core_ai", "core", "ai"),
        ("question/engine/lib.php", "core_question", "core", "question"),
        ("admin/cli/cron.php", "core_admin", "core", "admin"),
    ],
)
def test_infer_component_for_common_moodle_paths(
    path: str,
    expected_name: str,
    expected_type: str,
    expected_root: str,
) -> None:
    component = infer_component(path)
    assert component.name == expected_name
    assert component.component_type == expected_type
    assert component.root_path == expected_root


def test_infer_component_uses_subplugin_mounts_for_child_plugin_paths() -> None:
    component = infer_component(
        "mod/forum/report/summary/db/access.php",
        subplugin_mounts=[
            SubpluginMount(
                subtype="forumreport",
                parent_component="mod_forum",
                parent_root_path="mod/forum",
                mount_path="mod/forum/report",
            )
        ],
    )

    assert component.name == "forumreport_summary"
    assert component.component_type == "forumreport"
    assert component.root_path == "mod/forum/report/summary"


@pytest.mark.parametrize(
    ("path", "expected_role"),
    [
        ("mod/forum/version.php", "version_file"),
        ("mod/forum/lib.php", "lib_file"),
        ("mod/forum/locallib.php", "locallib_file"),
        ("admin/tool/demo/settings.php", "settings_file"),
        ("mod/forum/renderer.php", "renderer_file"),
        ("mod/forum/classes/output/discussion_list.php", "output_class"),
        ("mod/forum/classes/external/discussion_exporter.php", "external_api_class"),
        ("mod/forum/classes/task/cleanup_task.php", "task_class"),
        ("mod/forum/db/access.php", "access_definition"),
        ("mod/forum/db/services.php", "services_definition"),
        ("mod/forum/db/events.php", "events_definition"),
        ("mod/forum/db/tasks.php", "tasks_definition"),
        ("mod/forum/db/install.xml", "install_xml"),
        ("mod/forum/db/upgrade.php", "upgrade_file"),
        ("mod/forum/lang/en/mod_forum.php", "lang_file"),
        ("mod/forum/templates/discussion_list.mustache", "template_file"),
        ("mod/forum/amd/src/forum.js", "amd_source"),
        ("mod/forum/amd/build/forum.min.js", "amd_build"),
        ("mod/forum/tests/discussion_exporter_test.php", "phpunit_test"),
        ("mod/forum/tests/behat/manage_discussions.feature", "behat_feature"),
        ("mod/forum/tests/behat/behat_mod_forum.php", "behat_context"),
        ("mod/forum/classes/observer.php", "unknown"),
    ],
)
def test_classify_common_moodle_file_roles(path: str, expected_role: str) -> None:
    assert classify_file_role(path) == expected_role

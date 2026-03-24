"""Tests for Moodle component inference and file-role rules."""

from moodle_indexer.components import infer_component
from moodle_indexer.file_roles import classify_file_role


def test_infer_component_for_module_and_admin_tool() -> None:
    assert infer_component("mod/forum/lib.php").name == "mod_forum"
    assert infer_component("admin/tool/demo/settings.php").name == "tool_demo"


def test_classify_common_moodle_file_roles() -> None:
    assert classify_file_role("mod/forum/version.php") == "version_file"
    assert classify_file_role("mod/forum/db/access.php") == "access_definition"
    assert classify_file_role("mod/forum/lang/en/mod_forum.php") == "lang_file"
    assert classify_file_role("mod/forum/tests/behat/behat_mod_forum.php") == "behat_context"
    assert classify_file_role("mod/forum/tests/discussion_exporter_test.php") == "phpunit_test"

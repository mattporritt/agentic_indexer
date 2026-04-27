# Copyright (c) Moodle Pty Ltd. All rights reserved.
# Licensed under the Moodle Community License v1.3.
# See LICENSE.md in the repository root for full terms.
# Commercial use requires a separate written agreement with Moodle.

"""Path-based Moodle file-role classification.

The classifier uses deterministic rules because Moodle has strong file-layout
conventions and those conventions are valuable retrieval signals.
"""

from __future__ import annotations


def classify_file_role(relative_path: str) -> str:
    """Classify a repository-relative path into a Moodle file role."""

    path = relative_path.lower()
    if path.endswith("/version.php"):
        return "version_file"
    if path.endswith("/lib.php"):
        return "lib_file"
    if path.endswith("/locallib.php"):
        return "locallib_file"
    if path.endswith("/settings.php"):
        return "settings_file"
    if path.endswith("/db/access.php"):
        return "access_definition"
    if path.endswith("/db/services.php"):
        return "services_definition"
    if path.endswith("/db/events.php"):
        return "events_definition"
    if path.endswith("/db/tasks.php"):
        return "tasks_definition"
    if path.endswith("/db/upgrade.php"):
        return "upgrade_file"
    if path.endswith("/db/install.xml"):
        return "install_xml"
    if "/classes/output/" in path:
        return "output_class"
    if path.endswith("/renderer.php") or "/classes/output/" in path:
        return "renderer_file" if path.endswith("/renderer.php") else "output_class"
    if "/classes/external/" in path:
        return "external_api_class"
    if "/classes/task/" in path:
        return "task_class"
    if "behat" in path and path.endswith(".php"):
        return "behat_context"
    if path.endswith(".feature"):
        return "behat_feature"
    if "/lang/en/" in path and path.endswith(".php"):
        return "lang_file"
    if "/templates/" in path and path.endswith(".mustache"):
        return "template_file"
    if "/scss/" in path and path.endswith(".scss"):
        return "scss_source"
    if "/amd/src/" in path and path.endswith(".js"):
        return "amd_source"
    if "/amd/build/" in path and path.endswith(".min.js"):
        return "amd_build"
    if path.endswith("_test.php") or "/tests/" in path and path.endswith(".php"):
        return "phpunit_test"
    return "unknown"

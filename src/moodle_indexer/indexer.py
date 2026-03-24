"""Full-rebuild indexing pipeline.

This module orchestrates repository scanning, Moodle-specific inference,
extraction, and SQLite persistence for the Phase 1 MVP.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

from moodle_indexer.components import infer_component
from moodle_indexer.config import IndexConfig
from moodle_indexer.extractors import (
    extract_capabilities,
    extract_capability_usages,
    extract_language_string_usages,
    extract_language_strings,
    extract_php_artifacts,
    extract_tests,
    is_php_file,
)
from moodle_indexer.file_roles import classify_file_role
from moodle_indexer.paths import normalize_relative_path
from moodle_indexer.scanner import scan_repository
from moodle_indexer.store import (
    initialize_database,
    insert_capability,
    insert_capability_usage,
    insert_component,
    insert_file,
    insert_language_string,
    insert_language_string_usage,
    insert_relationship,
    insert_repository,
    insert_symbol,
    insert_test,
)


LOGGER = logging.getLogger(__name__)


def build_index(config: IndexConfig) -> dict[str, int | str]:
    """Build a fresh SQLite index for the target Moodle repository."""

    repository_root = config.moodle_root.resolve(strict=True)
    LOGGER.info("Scanning Moodle repository at %s", repository_root)
    files = scan_repository(repository_root)
    LOGGER.info("Discovered %d candidate files", len(files))

    connection = initialize_database(config.database_path)
    repository_id = insert_repository(connection, str(repository_root))

    component_cache: dict[str, int] = {}
    counts = {
        "files": 0,
        "components": 0,
        "symbols": 0,
        "relationships": 0,
        "capabilities": 0,
        "language_strings": 0,
        "tests": 0,
    }

    for file_path in files:
        relative_path = normalize_relative_path(repository_root, file_path)
        component = infer_component(relative_path)
        file_role = classify_file_role(relative_path)
        if component.name not in component_cache:
            component_id = insert_component(
                connection,
                repository_id=repository_id,
                name=component.name,
                component_type=component.component_type,
                root_path=component.root_path,
            )
            component_cache[component.name] = component_id
            counts["components"] += 1
        component_id = component_cache[component.name]

        file_id = insert_file(
            connection,
            repository_id=repository_id,
            component_id=component_id,
            relative_path=relative_path,
            absolute_path=str(file_path.resolve()),
            file_role=file_role,
            extension=file_path.suffix.lower(),
        )
        counts["files"] += 1

        source = file_path.read_text(encoding="utf-8", errors="ignore")

        if is_php_file(file_path):
            symbols, relationships = extract_php_artifacts(source, relative_path, component.name)
            for symbol in symbols:
                insert_symbol(connection, file_id, component_id, asdict(symbol))
            for relationship in relationships:
                insert_relationship(connection, file_id, asdict(relationship))
            counts["symbols"] += len(symbols)
            counts["relationships"] += len(relationships)

            capabilities = extract_capabilities(source, relative_path, component.name)
            for capability in capabilities:
                insert_capability(connection, file_id, component_id, asdict(capability))
            counts["capabilities"] += len(capabilities)

            capability_usages = extract_capability_usages(source, relative_path, component.name)
            for usage in capability_usages:
                insert_capability_usage(connection, file_id, component_id, asdict(usage))

            language_string_usages = extract_language_string_usages(source, relative_path)
            for usage in language_string_usages:
                insert_language_string_usage(connection, file_id, asdict(usage))

            tests = extract_tests(source, relative_path, component.name)
            for test_record in tests:
                insert_test(connection, file_id, component_id, asdict(test_record))
            counts["tests"] += len(tests)

        strings = extract_language_strings(source, relative_path, component.name)
        for language_string in strings:
            insert_language_string(connection, file_id, component_id, asdict(language_string))
        counts["language_strings"] += len(strings)

        if file_role in {"behat_feature", "behat_context"} and not is_php_file(file_path):
            tests = extract_tests(source, relative_path, component.name)
            for test_record in tests:
                insert_test(connection, file_id, component_id, asdict(test_record))
            counts["tests"] += len(tests)

    connection.commit()
    connection.close()

    return {
        "repository": str(config.moodle_root),
        "database": str(config.database_path),
        **counts,
    }

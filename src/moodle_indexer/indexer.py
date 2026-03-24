"""Full-rebuild indexing pipeline.

This module orchestrates repository scanning, Moodle-specific inference,
extraction, and SQLite persistence for the Phase 1 MVP.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
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
from moodle_indexer.paths import build_indexed_paths
from moodle_indexer.progress import ProgressBar
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

    repository_root = config.repository_root.resolve(strict=True)
    application_root = config.application_root.resolve(strict=True)
    LOGGER.info("Scanning Moodle repository at %s", repository_root)
    LOGGER.info("Application root detected at %s", application_root)
    files = scan_repository(repository_root)
    LOGGER.info("Discovered %d candidate files", len(files))
    LOGGER.info("Using %d worker(s) for extraction", config.workers)

    connection = initialize_database(config.database_path)
    repository_id = insert_repository(
        connection,
        input_path=config.input_path,
        repository_root=str(repository_root),
        application_root=str(application_root),
        layout_type=config.layout_type,
    )

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

    progress = ProgressBar(total=len(files))
    try:
        for file_payload in _iter_file_payloads(repository_root, application_root, files, config.workers):
            repository_relative_path = file_payload["repository_relative_path"]
            moodle_path = file_payload["moodle_path"]
            component = file_payload["component"]
            file_role = file_payload["file_role"]
            absolute_path = file_payload["absolute_path"]
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
                repository_relative_path=repository_relative_path,
                moodle_path=moodle_path,
                path_scope=file_payload["path_scope"],
                absolute_path=absolute_path,
                file_role=file_role,
                extension=file_payload["extension"],
            )
            counts["files"] += 1

            for symbol in file_payload["symbols"]:
                insert_symbol(connection, file_id, component_id, asdict(symbol))
            for relationship in file_payload["relationships"]:
                insert_relationship(connection, file_id, asdict(relationship))
            counts["symbols"] += len(file_payload["symbols"])
            counts["relationships"] += len(file_payload["relationships"])

            for capability in file_payload["capabilities"]:
                insert_capability(connection, file_id, component_id, asdict(capability))
            counts["capabilities"] += len(file_payload["capabilities"])

            for usage in file_payload["capability_usages"]:
                insert_capability_usage(connection, file_id, component_id, asdict(usage))

            for usage in file_payload["language_string_usages"]:
                insert_language_string_usage(connection, file_id, asdict(usage))

            for test_record in file_payload["tests"]:
                insert_test(connection, file_id, component_id, asdict(test_record))
            counts["tests"] += len(file_payload["tests"])

            for language_string in file_payload["language_strings"]:
                insert_language_string(connection, file_id, component_id, asdict(language_string))
            counts["language_strings"] += len(file_payload["language_strings"])
            progress.advance()
    finally:
        progress.close()

    connection.commit()
    connection.close()

    return {
        "repository": str(repository_root),
        "input_path": config.input_path,
        "repository_root": str(repository_root),
        "application_root": str(application_root),
        "layout_type": config.layout_type,
        "database": str(config.database_path),
        **counts,
    }


def _iter_file_payloads(
    repository_root: Path,
    application_root: Path,
    files: list[Path],
    workers: int,
):
    """Yield extracted file payloads in input order.

    Extraction work benefits from concurrent file I/O and parsing, while the
    main process keeps SQLite writes serialized and deterministic.
    """

    if workers == 1:
        for file_path in files:
            yield _process_file_for_indexing(repository_root, application_root, file_path)
        return

    max_workers = max(1, min(workers, len(files), os.cpu_count() or 1))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        yield from executor.map(
            _process_file_for_indexing,
            [repository_root] * len(files),
            [application_root] * len(files),
            files,
            chunksize=32,
        )


def _process_file_for_indexing(repository_root: Path, application_root: Path, file_path: Path) -> dict:
    """Extract all indexable artifacts for one file."""

    indexed_paths = build_indexed_paths(repository_root, application_root, file_path)
    component = infer_component(indexed_paths.moodle_path)
    file_role = classify_file_role(indexed_paths.moodle_path)
    source = file_path.read_text(encoding="utf-8", errors="ignore")

    symbols = []
    relationships = []
    capabilities = []
    capability_usages = []
    language_string_usages = []
    tests = []

    if is_php_file(file_path):
        symbols, relationships = extract_php_artifacts(source, indexed_paths.moodle_path, component.name)
        capabilities = extract_capabilities(source, indexed_paths.moodle_path, component.name)
        capability_usages = extract_capability_usages(source, indexed_paths.moodle_path, component.name)
        language_string_usages = extract_language_string_usages(source, indexed_paths.moodle_path)
        tests = extract_tests(source, indexed_paths.moodle_path, component.name)

    language_strings = extract_language_strings(source, indexed_paths.moodle_path, component.name)
    if file_role in {"behat_feature", "behat_context"} and not is_php_file(file_path):
        tests = extract_tests(source, indexed_paths.moodle_path, component.name)

    return {
        "absolute_path": str(file_path.resolve()),
        "capabilities": capabilities,
        "capability_usages": capability_usages,
        "component": component,
        "extension": file_path.suffix.lower(),
        "file_role": file_role,
        "language_strings": language_strings,
        "language_string_usages": language_string_usages,
        "moodle_path": indexed_paths.moodle_path,
        "path_scope": indexed_paths.path_scope,
        "repository_relative_path": indexed_paths.repository_relative_path,
        "relationships": relationships,
        "symbols": symbols,
        "tests": tests,
    }

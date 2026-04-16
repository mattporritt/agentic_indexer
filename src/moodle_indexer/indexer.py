# Copyright (c) Moodle Pty Ltd. All rights reserved.
# Licensed under the Moodle Community License v1.3.
# See LICENSE.md in the repository root for full terms.
# Commercial use requires a separate written agreement with Moodle.

"""Full-rebuild indexing pipeline.

This module orchestrates repository scanning, Moodle-specific inference,
extraction, and SQLite persistence for the current v1 code-intelligence layer.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Any

from moodle_indexer.components import infer_component
from moodle_indexer.config import IndexConfig
from moodle_indexer.extractors import (
    extract_capabilities,
    extract_capability_usages,
    extract_js_module_artifacts,
    extract_language_string_usages,
    extract_language_strings,
    extract_php_artifacts,
    extract_tests,
    extract_webservices,
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
    insert_js_import,
    insert_js_module,
    insert_language_string,
    insert_language_string_usage,
    insert_relationship,
    insert_repository,
    insert_symbol,
    insert_test,
    insert_webservice,
)
from moodle_indexer.subplugins import load_subplugin_mounts


LOGGER = logging.getLogger(__name__)
FAILURE_SAMPLE_LIMIT = 5


def build_index(config: IndexConfig) -> dict[str, int | str]:
    """Build a fresh SQLite index for the target Moodle repository."""

    started_at = time.perf_counter()
    repository_root = config.repository_root.resolve(strict=True)
    application_root = config.application_root.resolve(strict=True)
    LOGGER.info("Scanning repository at %s", repository_root)
    LOGGER.info("Application root: %s (%s layout)", application_root, config.layout_type)

    scan_started_at = time.perf_counter()
    scan_result = scan_repository(repository_root)
    scan_seconds = time.perf_counter() - scan_started_at
    files = scan_result.files
    discovered_files = len(files)
    subplugin_mounts = load_subplugin_mounts(application_root)
    LOGGER.info(
        "Discovered %d candidate files in %.2fs (%d ignored during scan)",
        discovered_files,
        scan_seconds,
        scan_result.ignored_files,
    )
    if subplugin_mounts:
        LOGGER.info("Loaded %d subplugin mount(s) from db/subplugins.json", len(subplugin_mounts))

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
        "webservices": 0,
        "js_modules": 0,
        "js_imports": 0,
        "tests": 0,
    }
    worker_stats = _resolve_worker_usage(config.workers, discovered_files)
    failure_examples: list[dict[str, str]] = []
    processed_files = 0
    failed_files = 0
    skipped_files = 0
    persisted_files = 0
    persistence_seconds = 0.0

    LOGGER.info(
        "Extraction mode: %s (requested workers=%d, active workers=%d, tasks=%d)",
        worker_stats["mode"],
        worker_stats["requested_workers"],
        worker_stats["active_workers"],
        worker_stats["tasks_submitted"],
    )
    if worker_stats["mode"] == "parallel":
        LOGGER.info("SQLite persistence remains serial in the main process.")

    extraction_progress = ProgressBar(total=discovered_files, label="Parsing/extracting files")
    persistence_progress = ProgressBar(total=discovered_files, label="Persisting records")
    extraction_started_at = time.perf_counter()
    try:
        for result in _iter_file_payloads(
            repository_root,
            application_root,
            files,
            subplugin_mounts,
            worker_stats["active_workers"],
        ):
            extraction_progress.advance()
            if result["status"] == "failed":
                failed_files += 1
                if len(failure_examples) < FAILURE_SAMPLE_LIMIT:
                    failure_examples.append(
                        {
                            "file": result["file"],
                            "error": result["error"],
                        }
                    )
                continue

            processed_files += 1
            file_payload = result["payload"]
            persist_started_at = time.perf_counter()
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

            for webservice in file_payload["webservices"]:
                insert_webservice(connection, file_id, component_id, asdict(webservice))
            counts["webservices"] += len(file_payload["webservices"])

            if file_payload["js_module"] is not None:
                js_module_id = insert_js_module(connection, file_id, component_id, asdict(file_payload["js_module"]))
                counts["js_modules"] += 1
                for js_import in file_payload["js_imports"]:
                    insert_js_import(connection, js_module_id, asdict(js_import))
                counts["js_imports"] += len(file_payload["js_imports"])

            for test_record in file_payload["tests"]:
                insert_test(connection, file_id, component_id, asdict(test_record))
            counts["tests"] += len(file_payload["tests"])

            for language_string in file_payload["language_strings"]:
                insert_language_string(connection, file_id, component_id, asdict(language_string))
            counts["language_strings"] += len(file_payload["language_strings"])
            persisted_files += 1
            persistence_seconds += time.perf_counter() - persist_started_at
            persistence_progress.advance()
    finally:
        extraction_progress.close()
        persistence_progress.close()

    pipeline_seconds = time.perf_counter() - extraction_started_at
    connection.commit()
    connection.close()
    total_seconds = time.perf_counter() - started_at

    LOGGER.info(
        "Completed indexing in %.2fs: discovered=%d processed=%d persisted=%d skipped=%d failed=%d",
        total_seconds,
        discovered_files,
        processed_files,
        persisted_files,
        skipped_files,
        failed_files,
    )
    LOGGER.info(
        "Phase timings: scan=%.2fs pipeline=%.2fs persistence=%.2fs total=%.2fs",
        scan_seconds,
        pipeline_seconds,
        persistence_seconds,
        total_seconds,
    )
    if failure_examples:
        LOGGER.warning("Sample failures: %s", failure_examples)

    return {
        "repository": str(repository_root),
        "input_path": config.input_path,
        "repository_root": str(repository_root),
        "application_root": str(application_root),
        "layout_type": config.layout_type,
        "database": str(config.database_path),
        "discovered_files": discovered_files,
        "processed_files": processed_files,
        "persisted_files": persisted_files,
        "skipped_files": skipped_files,
        "failed_files": failed_files,
        "ignored_files": scan_result.ignored_files,
        "worker_usage": worker_stats,
        "timings": {
            "scan_seconds": round(scan_seconds, 4),
            "pipeline_seconds": round(pipeline_seconds, 4),
            "persistence_seconds": round(persistence_seconds, 4),
            "total_seconds": round(total_seconds, 4),
        },
        "failure_examples": failure_examples,
        **counts,
    }


def _iter_file_payloads(
    repository_root: Path,
    application_root: Path,
    files: list[Path],
    subplugin_mounts,
    workers: int,
):
    """Yield extraction results in input order.

    Extraction work benefits from concurrent file I/O and parsing, while the
    main process keeps SQLite writes serialized and deterministic.
    """

    if workers <= 1 or len(files) <= 1:
        for file_path in files:
            try:
                yield {
                    "status": "ok",
                    "payload": _process_file_for_indexing(
                        repository_root,
                        application_root,
                        file_path,
                        subplugin_mounts,
                    ),
                }
            except Exception as exc:  # pragma: no cover - exercised via monkeypatch tests.
                yield {
                    "status": "failed",
                    "file": str(file_path),
                    "error": f"{type(exc).__name__}: {exc}",
                }
        return

    pending_results: dict[int, dict[str, Any]] = {}
    next_index = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _process_file_for_indexing,
                repository_root,
                application_root,
                file_path,
                subplugin_mounts,
            ): (index, file_path)
            for index, file_path in enumerate(files)
        }
        for future in as_completed(futures):
            index, file_path = futures[future]
            try:
                pending_results[index] = {
                    "status": "ok",
                    "payload": future.result(),
                }
            except Exception as exc:  # pragma: no cover - exercised via monkeypatch tests.
                pending_results[index] = {
                    "status": "failed",
                    "file": str(file_path),
                    "error": f"{type(exc).__name__}: {exc}",
                }

            while next_index in pending_results:
                yield pending_results.pop(next_index)
                next_index += 1


def _process_file_for_indexing(repository_root: Path, application_root: Path, file_path: Path, subplugin_mounts) -> dict:
    """Extract all indexable artifacts for one file."""

    indexed_paths = build_indexed_paths(repository_root, application_root, file_path)
    component = infer_component(indexed_paths.moodle_path, subplugin_mounts=subplugin_mounts)
    file_role = classify_file_role(indexed_paths.moodle_path)
    source = file_path.read_text(encoding="utf-8", errors="ignore")

    symbols = []
    relationships = []
    capabilities = []
    capability_usages = []
    language_string_usages = []
    tests = []
    webservices = []
    js_module = None
    js_imports = []

    if is_php_file(file_path):
        symbols, relationships = extract_php_artifacts(source, indexed_paths.moodle_path, component.name)
        capabilities = extract_capabilities(source, indexed_paths.moodle_path, component.name)
        capability_usages = extract_capability_usages(source, indexed_paths.moodle_path, component.name)
        language_string_usages = extract_language_string_usages(source, indexed_paths.moodle_path)
        webservices = extract_webservices(source, indexed_paths.moodle_path, component.name)
        tests = extract_tests(source, indexed_paths.moodle_path, component.name)
    elif file_path.suffix.lower() == ".js":
        js_module, js_imports, js_relationships = extract_js_module_artifacts(
            source,
            indexed_paths.moodle_path,
            component.name,
        )
        relationships.extend(js_relationships)

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
        "js_imports": js_imports,
        "js_module": js_module,
        "symbols": symbols,
        "tests": tests,
        "webservices": webservices,
    }


def _resolve_worker_usage(requested_workers: int, task_count: int) -> dict[str, int | str]:
    """Return lightweight diagnostics about the configured extraction workers."""

    active_workers = max(1, min(requested_workers, task_count or 1, os.cpu_count() or 1))
    return {
        "requested_workers": requested_workers,
        "active_workers": active_workers,
        "tasks_submitted": task_count,
        "mode": "serial" if active_workers == 1 else "parallel",
    }

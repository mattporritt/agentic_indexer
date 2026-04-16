# Copyright (c) Moodle Pty Ltd. All rights reserved.
# Licensed under the Moodle Community License v1.3.
# See LICENSE.md in the repository root for full terms.
# Commercial use requires a separate written agreement with Moodle.

"""Command-line interface for the Moodle AI indexer.

The CLI is intentionally compact and JSON-oriented for machine consumers while
still providing progress logging for humans during index builds.

It exposes the project's public surface in one place:

- structural lookup
- bounded navigation
- semantic retrieval
- planning and safety
- compact context-bundle packaging
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from moodle_indexer.config import build_index_config
from moodle_indexer.errors import IndexerError
from moodle_indexer.indexer import build_index
from moodle_indexer.json_output import dumps_json, error_payload, success_payload
from moodle_indexer.queries import (
    assess_test_impact,
    build_context_bundle,
    component_summary,
    dependency_neighborhood,
    execution_guardrails,
    file_context,
    find_definition,
    find_related_definitions,
    find_symbol,
    propose_change_plan,
    semantic_context,
    suggest_edit_surface,
    suggest_related,
)
from moodle_indexer.runtime_contract import build_runtime_contract
from moodle_indexer.store import open_database


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level CLI parser."""

    parser = argparse.ArgumentParser(
        prog="moodle-indexer",
        description="Build and query a bounded Moodle code-intelligence index for a local checkout.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser("index", help="Build or rebuild the SQLite index.")
    index_parser.add_argument("--moodle-path", required=True, help="Path to the local Moodle checkout.")
    index_parser.add_argument("--db-path", required=True, help="Path to the SQLite database file to create.")
    index_parser.add_argument(
        "--workers",
        type=int,
        default=max(1, min(8, os.cpu_count() or 1)),
        help="Number of worker threads to use for parallel extraction. SQLite persistence stays serial.",
    )

    find_parser = subparsers.add_parser("find-symbol", help="Find symbol definitions by name or fully qualified name.")
    find_parser.add_argument("--db-path", required=True, help="Path to an existing SQLite index.")
    find_parser.add_argument("--symbol", required=True, help="Symbol name or fully qualified name.")

    definition_parser = subparsers.add_parser(
        "find-definition",
        help="Return IDE-style definition details for a function, method, or class.",
    )
    definition_parser.add_argument("--db-path", required=True, help="Path to an existing SQLite index.")
    definition_parser.add_argument("--symbol", required=True, help="Function, class, or method query such as get_string or assign::view.")
    definition_parser.add_argument(
        "--type",
        choices=["any", "function", "method", "class", "interface", "trait", "js_module"],
        default="any",
        help="Optional symbol-type filter.",
    )
    definition_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of matches to return.",
    )
    definition_parser.add_argument(
        "--include-usages",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include a small number of usage examples.",
    )
    definition_parser.add_argument(
        "--json-contract",
        action="store_true",
        help="Emit the stable runtime-facing JSON contract envelope.",
    )

    related_definitions_parser = subparsers.add_parser(
        "find-related-definitions",
        help="Return bounded related definitions and artifacts around a symbol or file.",
    )
    related_definitions_parser.add_argument("--db-path", required=True, help="Path to an existing SQLite index.")
    related_definitions_target = related_definitions_parser.add_mutually_exclusive_group(required=True)
    related_definitions_target.add_argument("--symbol", help="Definition query such as assign::view or core/ajax.")
    related_definitions_target.add_argument(
        "--file",
        help="Moodle-native, repository-relative, or absolute file path.",
    )
    related_definitions_parser.add_argument(
        "--limit",
        type=int,
        default=12,
        help="Maximum number of primary or secondary related items to return.",
    )

    edit_surface_parser = subparsers.add_parser(
        "suggest-edit-surface",
        help="Return the likely primary and secondary edit surface around a symbol or file.",
    )
    edit_surface_parser.add_argument("--db-path", required=True, help="Path to an existing SQLite index.")
    edit_surface_target = edit_surface_parser.add_mutually_exclusive_group(required=True)
    edit_surface_target.add_argument("--symbol", help="Definition query such as assign::view or core/ajax.")
    edit_surface_target.add_argument(
        "--file",
        help="Moodle-native, repository-relative, or absolute file path.",
    )
    edit_surface_parser.add_argument(
        "--limit",
        type=int,
        default=12,
        help="Maximum number of primary or secondary edit-surface items to return.",
    )

    dependency_parser = subparsers.add_parser(
        "dependency-neighborhood",
        help="Return a bounded, confidence-aware dependency neighborhood around a symbol or file.",
    )
    dependency_parser.add_argument("--db-path", required=True, help="Path to an existing SQLite index.")
    dependency_target = dependency_parser.add_mutually_exclusive_group(required=True)
    dependency_target.add_argument("--symbol", help="Definition query such as assign::view or core/ajax.")
    dependency_target.add_argument(
        "--file",
        help="Moodle-native, repository-relative, or absolute file path.",
    )
    dependency_parser.add_argument(
        "--limit",
        type=int,
        default=8,
        help="Maximum number of items to return per dependency-neighborhood section.",
    )

    semantic_parser = subparsers.add_parser(
        "semantic-context",
        help="Return bounded hybrid semantic context around a symbol, file, or free-text query.",
    )
    semantic_parser.add_argument("--db-path", required=True, help="Path to an existing SQLite index.")
    semantic_target = semantic_parser.add_mutually_exclusive_group(required=True)
    semantic_target.add_argument("--symbol", help="Definition query such as assign::view or core/ajax.")
    semantic_target.add_argument(
        "--file",
        help="Moodle-native, repository-relative, or absolute file path.",
    )
    semantic_target.add_argument(
        "--query",
        help="Free-text retrieval query such as examples of Moodle external API methods with PHPUnit coverage.",
    )
    semantic_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of primary or secondary semantic-context items to return.",
    )
    semantic_parser.add_argument(
        "--json-contract",
        action="store_true",
        help="Emit the stable runtime-facing JSON contract envelope.",
    )

    change_plan_parser = subparsers.add_parser(
        "propose-change-plan",
        help="Return a bounded, confidence-aware proposed edit set and change plan.",
    )
    change_plan_parser.add_argument("--db-path", required=True, help="Path to an existing SQLite index.")
    change_plan_target = change_plan_parser.add_mutually_exclusive_group(required=True)
    change_plan_target.add_argument("--symbol", help="Definition query such as assign::view or core/ajax.")
    change_plan_target.add_argument(
        "--file",
        help="Moodle-native, repository-relative, or absolute file path.",
    )
    change_plan_target.add_argument(
        "--query",
        help="Free-text change goal such as add a parameter to a Moodle external API method and update its tests.",
    )
    change_plan_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of required, likely, or optional plan items to return.",
    )

    test_impact_parser = subparsers.add_parser(
        "assess-test-impact",
        help="Return a bounded test-impact and validation view around a symbol, file, or change goal.",
    )
    test_impact_parser.add_argument("--db-path", required=True, help="Path to an existing SQLite index.")
    test_impact_target = test_impact_parser.add_mutually_exclusive_group(required=True)
    test_impact_target.add_argument("--symbol", help="Definition query such as assign::view or core/ajax.")
    test_impact_target.add_argument(
        "--file",
        help="Moodle-native, repository-relative, or absolute file path.",
    )
    test_impact_target.add_argument(
        "--query",
        help="Free-text change goal such as add a parameter to a Moodle external API method and update its tests.",
    )
    test_impact_parser.add_argument(
        "--limit",
        type=int,
        default=8,
        help="Maximum number of direct, likely, or review/test-impact items to return.",
    )

    guardrails_parser = subparsers.add_parser(
        "execution-guardrails",
        help="Return bounded risk, pre/post checks, and execution guardrails around a symbol, file, or change goal.",
    )
    guardrails_parser.add_argument("--db-path", required=True, help="Path to an existing SQLite index.")
    guardrails_target = guardrails_parser.add_mutually_exclusive_group(required=True)
    guardrails_target.add_argument("--symbol", help="Definition query such as assign::view or core/ajax.")
    guardrails_target.add_argument(
        "--file",
        help="Moodle-native, repository-relative, or absolute file path.",
    )
    guardrails_target.add_argument(
        "--query",
        help="Free-text change goal such as add a parameter to a Moodle external API method and update its tests.",
    )
    guardrails_parser.add_argument(
        "--limit",
        type=int,
        default=8,
        help="Maximum number of guardrail items to return per section.",
    )

    bundle_parser = subparsers.add_parser(
        "build-context-bundle",
        help="Return a compact, agent-usable context bundle around a symbol, file, or change goal.",
    )
    bundle_parser.add_argument("--db-path", required=True, help="Path to an existing SQLite index.")
    bundle_target = bundle_parser.add_mutually_exclusive_group(required=True)
    bundle_target.add_argument("--symbol", help="Definition query such as assign::view or core/ajax.")
    bundle_target.add_argument(
        "--file",
        help="Moodle-native, repository-relative, or absolute file path.",
    )
    bundle_target.add_argument(
        "--query",
        help="Free-text change goal such as add a parameter to a Moodle external API method and update its tests.",
    )
    bundle_parser.add_argument(
        "--limit",
        type=int,
        default=8,
        help="Maximum number of supporting or optional bundle items to return.",
    )
    bundle_parser.add_argument(
        "--json-contract",
        action="store_true",
        help="Emit the stable runtime-facing JSON contract envelope.",
    )

    file_parser = subparsers.add_parser("file-context", help="Return indexed metadata for one file.")
    file_parser.add_argument("--db-path", required=True, help="Path to an existing SQLite index.")
    file_parser.add_argument(
        "--file",
        required=True,
        help="Moodle-native, repository-relative, or absolute file path.",
    )

    component_parser = subparsers.add_parser("component-summary", help="Summarize one Moodle component.")
    component_parser.add_argument("--db-path", required=True, help="Path to an existing SQLite index.")
    component_parser.add_argument("--component", required=True, help="Component name such as mod_forum.")

    related_parser = subparsers.add_parser("suggest-related", help="Suggest likely companion files.")
    related_parser.add_argument("--db-path", required=True, help="Path to an existing SQLite index.")
    related_parser.add_argument(
        "--file",
        required=True,
        help="Moodle-native, repository-relative, or absolute file path.",
    )

    return parser


def configure_logging() -> None:
    """Configure user-friendly progress logging for CLI execution."""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", force=True)


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and print JSON responses."""

    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "index":
            payload = run_index(args.moodle_path, args.db_path, args.workers)
        elif args.command == "find-symbol":
            payload = run_find_symbol(args.db_path, args.symbol)
        elif args.command == "find-definition":
            payload = run_find_definition(
                args.db_path,
                args.symbol,
                args.type,
                args.limit,
                args.include_usages,
                json_contract=args.json_contract,
            )
        elif args.command == "find-related-definitions":
            payload = run_find_related_definitions(args.db_path, args.symbol, args.file, args.limit)
        elif args.command == "suggest-edit-surface":
            payload = run_suggest_edit_surface(args.db_path, args.symbol, args.file, args.limit)
        elif args.command == "dependency-neighborhood":
            payload = run_dependency_neighborhood(args.db_path, args.symbol, args.file, args.limit)
        elif args.command == "semantic-context":
            payload = run_semantic_context(
                args.db_path,
                args.symbol,
                args.file,
                args.query,
                args.limit,
                json_contract=args.json_contract,
            )
        elif args.command == "propose-change-plan":
            payload = run_propose_change_plan(args.db_path, args.symbol, args.file, args.query, args.limit)
        elif args.command == "assess-test-impact":
            payload = run_assess_test_impact(args.db_path, args.symbol, args.file, args.query, args.limit)
        elif args.command == "execution-guardrails":
            payload = run_execution_guardrails(args.db_path, args.symbol, args.file, args.query, args.limit)
        elif args.command == "build-context-bundle":
            payload = run_build_context_bundle(
                args.db_path,
                args.symbol,
                args.file,
                args.query,
                args.limit,
                json_contract=args.json_contract,
            )
        elif args.command == "file-context":
            payload = run_file_context(args.db_path, args.file)
        elif args.command == "component-summary":
            payload = run_component_summary(args.db_path, args.component)
        elif args.command == "suggest-related":
            payload = run_suggest_related(args.db_path, args.file)
        else:
            raise IndexerError(f"Unsupported command: {args.command}")
    except IndexerError as exc:
        if getattr(args, "json_contract", False) and args.command in {"find-definition", "semantic-context", "build-context-bundle"}:
            print(dumps_json(_runtime_error_contract(args, str(exc))))
            return 2
        print(dumps_json(error_payload(args.command, str(exc), error_type=type(exc).__name__)))
        return 2
    except FileNotFoundError as exc:
        if getattr(args, "json_contract", False) and args.command in {"find-definition", "semantic-context", "build-context-bundle"}:
            print(dumps_json(_runtime_error_contract(args, str(exc))))
            return 2
        print(dumps_json(error_payload(args.command, str(exc), error_type="FileNotFoundError")))
        return 2

    print(dumps_json(payload))
    return 0


def run_index(moodle_path: str, db_path: str, workers: int) -> dict:
    """Execute the ``index`` command."""

    config = build_index_config(moodle_path, db_path, workers=workers)
    result = build_index(config)
    return success_payload("index", result)


def run_find_symbol(db_path: str, symbol: str) -> dict:
    """Execute the ``find-symbol`` command."""

    connection = open_database(Path(db_path).expanduser().resolve())
    try:
        return success_payload("find-symbol", find_symbol(connection, symbol))
    finally:
        connection.close()


def run_find_definition(
    db_path: str,
    symbol: str,
    symbol_type: str,
    limit: int,
    include_usages: bool,
    *,
    json_contract: bool = False,
) -> dict:
    """Execute the ``find-definition`` command."""

    connection = open_database(Path(db_path).expanduser().resolve())
    try:
        data = find_definition(connection, symbol, symbol_type=symbol_type, limit=limit, include_usages=include_usages)
        if json_contract:
            return build_runtime_contract(
                command="find-definition",
                data=data,
                query=symbol,
                query_kind="symbol",
                limit=limit,
                symbol_type=symbol_type,
                include_usages=include_usages,
            )
        return success_payload("find-definition", data)
    finally:
        connection.close()


def run_find_related_definitions(db_path: str, symbol: str | None, file_path: str | None, limit: int) -> dict:
    """Execute the ``find-related-definitions`` command."""

    connection = open_database(Path(db_path).expanduser().resolve())
    try:
        return success_payload(
            "find-related-definitions",
            find_related_definitions(connection, symbol_query=symbol, file_path=file_path, limit=limit),
        )
    finally:
        connection.close()


def run_suggest_edit_surface(db_path: str, symbol: str | None, file_path: str | None, limit: int) -> dict:
    """Execute the ``suggest-edit-surface`` command."""

    connection = open_database(Path(db_path).expanduser().resolve())
    try:
        return success_payload(
            "suggest-edit-surface",
            suggest_edit_surface(connection, symbol_query=symbol, file_path=file_path, limit=limit),
        )
    finally:
        connection.close()


def run_dependency_neighborhood(db_path: str, symbol: str | None, file_path: str | None, limit: int) -> dict:
    """Execute the ``dependency-neighborhood`` command."""

    connection = open_database(Path(db_path).expanduser().resolve())
    try:
        return success_payload(
            "dependency-neighborhood",
            dependency_neighborhood(connection, symbol_query=symbol, file_path=file_path, limit=limit),
        )
    finally:
        connection.close()


def run_semantic_context(
    db_path: str,
    symbol: str | None,
    file_path: str | None,
    query_text: str | None,
    limit: int,
    *,
    json_contract: bool = False,
) -> dict:
    """Execute the ``semantic-context`` command."""

    connection = open_database(Path(db_path).expanduser().resolve())
    try:
        data = semantic_context(connection, symbol_query=symbol, file_path=file_path, query_text=query_text, limit=limit)
        if json_contract:
            return build_runtime_contract(
                command="semantic-context",
                data=data,
                query=symbol or file_path or query_text or "",
                query_kind=_runtime_query_kind(symbol=symbol, file_path=file_path, query_text=query_text),
                limit=limit,
            )
        return success_payload("semantic-context", data)
    finally:
        connection.close()


def run_file_context(db_path: str, file_path: str) -> dict:
    """Execute the ``file-context`` command."""

    connection = open_database(Path(db_path).expanduser().resolve())
    try:
        return success_payload("file-context", file_context(connection, file_path))
    finally:
        connection.close()


def run_propose_change_plan(
    db_path: str,
    symbol: str | None,
    file_path: str | None,
    query: str | None,
    limit: int,
) -> dict:
    """Execute the ``propose-change-plan`` command."""

    connection = open_database(Path(db_path).expanduser().resolve())
    try:
        return success_payload(
            "propose-change-plan",
            propose_change_plan(
                connection,
                symbol_query=symbol,
                file_path=file_path,
                query_text=query,
                limit=limit,
            ),
        )
    finally:
        connection.close()


def run_assess_test_impact(
    db_path: str,
    symbol: str | None,
    file_path: str | None,
    query_text: str | None,
    limit: int,
) -> dict:
    """Return bounded test-impact details around a symbol, file, or change goal."""

    connection = open_database(Path(db_path))
    try:
        return success_payload(
            "assess-test-impact",
            assess_test_impact(
                connection,
                symbol_query=symbol,
                file_path=file_path,
                query_text=query_text,
                limit=limit,
            ),
        )
    finally:
        connection.close()


def run_execution_guardrails(
    db_path: str,
    symbol: str | None,
    file_path: str | None,
    query_text: str | None,
    limit: int,
) -> dict:
    """Return bounded execution guardrails around a symbol, file, or change goal."""

    connection = open_database(Path(db_path))
    try:
        return success_payload(
            "execution-guardrails",
            execution_guardrails(
                connection,
                symbol_query=symbol,
                file_path=file_path,
                query_text=query_text,
                limit=limit,
            ),
        )
    finally:
        connection.close()


def run_build_context_bundle(
    db_path: str,
    symbol: str | None,
    file_path: str | None,
    query_text: str | None,
    limit: int,
    *,
    json_contract: bool = False,
) -> dict:
    """Return a compact agent-ready context bundle around a symbol, file, or goal."""

    connection = open_database(Path(db_path))
    try:
        data = build_context_bundle(
            connection,
            symbol_query=symbol,
            file_path=file_path,
            query_text=query_text,
            limit=limit,
        )
        if json_contract:
            return build_runtime_contract(
                command="build-context-bundle",
                data=data,
                query=symbol or file_path or query_text or "",
                query_kind=_runtime_query_kind(symbol=symbol, file_path=file_path, query_text=query_text),
                limit=limit,
            )
        return success_payload("build-context-bundle", data)
    finally:
        connection.close()


def run_component_summary(db_path: str, component: str) -> dict:
    """Execute the ``component-summary`` command."""

    connection = open_database(Path(db_path).expanduser().resolve())
    try:
        return success_payload("component-summary", component_summary(connection, component))
    finally:
        connection.close()


def _runtime_query_kind(*, symbol: str | None, file_path: str | None, query_text: str | None) -> str:
    """Return the stable runtime query-kind label for one command invocation."""

    if symbol:
        return "symbol"
    if file_path:
        return "file"
    return "query"


def _runtime_error_contract(args: argparse.Namespace, message: str) -> dict:
    """Return an empty runtime envelope for supported command errors."""

    query = getattr(args, "symbol", None) or getattr(args, "file", None) or getattr(args, "query", None) or ""
    return build_runtime_contract(
        command=args.command,
        data={},
        query=query,
        query_kind=_runtime_query_kind(
            symbol=getattr(args, "symbol", None),
            file_path=getattr(args, "file", None),
            query_text=getattr(args, "query", None),
        ),
        limit=int(getattr(args, "limit", 0) or 0),
        symbol_type=getattr(args, "type", None),
        include_usages=getattr(args, "include_usages", None),
    )


def run_suggest_related(db_path: str, file_path: str) -> dict:
    """Execute the ``suggest-related`` command."""

    connection = open_database(Path(db_path).expanduser().resolve())
    try:
        return success_payload(
            "suggest-related",
            suggest_related(connection, file_path),
        )
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())

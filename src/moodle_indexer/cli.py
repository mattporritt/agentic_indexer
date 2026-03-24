"""Command-line interface for the Moodle AI indexer.

The CLI is intentionally compact and JSON-oriented for machine consumers while
still providing progress logging for humans during index builds.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from moodle_indexer.config import build_index_config
from moodle_indexer.errors import IndexerError
from moodle_indexer.indexer import build_index
from moodle_indexer.json_output import dumps_json, error_payload, success_payload
from moodle_indexer.queries import component_summary, file_context, find_symbol, suggest_related
from moodle_indexer.store import open_database


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level CLI parser."""

    parser = argparse.ArgumentParser(
        prog="moodle-indexer",
        description="Build and query a Phase 1 SQLite index for a local Moodle checkout.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser("index", help="Build or rebuild the SQLite index.")
    index_parser.add_argument("--moodle-path", required=True, help="Path to the local Moodle checkout.")
    index_parser.add_argument("--db-path", required=True, help="Path to the SQLite database file to create.")

    find_parser = subparsers.add_parser("find-symbol", help="Find symbol definitions by name or fully qualified name.")
    find_parser.add_argument("--db-path", required=True, help="Path to an existing SQLite index.")
    find_parser.add_argument("--symbol", required=True, help="Symbol name or fully qualified name.")

    file_parser = subparsers.add_parser("file-context", help="Return indexed metadata for one file.")
    file_parser.add_argument("--db-path", required=True, help="Path to an existing SQLite index.")
    file_parser.add_argument("--moodle-path", required=True, help="Path to the indexed Moodle checkout.")
    file_parser.add_argument("--file", required=True, help="Repository-relative or absolute file path.")

    component_parser = subparsers.add_parser("component-summary", help="Summarize one Moodle component.")
    component_parser.add_argument("--db-path", required=True, help="Path to an existing SQLite index.")
    component_parser.add_argument("--component", required=True, help="Component name such as mod_forum.")

    related_parser = subparsers.add_parser("suggest-related", help="Suggest likely companion files.")
    related_parser.add_argument("--db-path", required=True, help="Path to an existing SQLite index.")
    related_parser.add_argument("--moodle-path", required=True, help="Path to the indexed Moodle checkout.")
    related_parser.add_argument("--file", required=True, help="Repository-relative or absolute file path.")

    return parser


def configure_logging() -> None:
    """Configure user-friendly progress logging for CLI execution."""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and print JSON responses."""

    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "index":
            payload = run_index(args.moodle_path, args.db_path)
        elif args.command == "find-symbol":
            payload = run_find_symbol(args.db_path, args.symbol)
        elif args.command == "file-context":
            payload = run_file_context(args.db_path, args.moodle_path, args.file)
        elif args.command == "component-summary":
            payload = run_component_summary(args.db_path, args.component)
        elif args.command == "suggest-related":
            payload = run_suggest_related(args.db_path, args.moodle_path, args.file)
        else:
            raise IndexerError(f"Unsupported command: {args.command}")
    except IndexerError as exc:
        print(dumps_json(error_payload(args.command, str(exc), error_type=type(exc).__name__)))
        return 2
    except FileNotFoundError as exc:
        print(dumps_json(error_payload(args.command, str(exc), error_type="FileNotFoundError")))
        return 2

    print(dumps_json(payload))
    return 0


def run_index(moodle_path: str, db_path: str) -> dict:
    """Execute the ``index`` command."""

    config = build_index_config(moodle_path, db_path)
    result = build_index(config)
    return success_payload("index", result)


def run_find_symbol(db_path: str, symbol: str) -> dict:
    """Execute the ``find-symbol`` command."""

    connection = open_database(Path(db_path).expanduser().resolve())
    try:
        return success_payload("find-symbol", find_symbol(connection, symbol))
    finally:
        connection.close()


def run_file_context(db_path: str, moodle_path: str, file_path: str) -> dict:
    """Execute the ``file-context`` command."""

    connection = open_database(Path(db_path).expanduser().resolve())
    try:
        return success_payload(
            "file-context",
            file_context(connection, Path(moodle_path).expanduser().resolve(), file_path),
        )
    finally:
        connection.close()


def run_component_summary(db_path: str, component: str) -> dict:
    """Execute the ``component-summary`` command."""

    connection = open_database(Path(db_path).expanduser().resolve())
    try:
        return success_payload("component-summary", component_summary(connection, component))
    finally:
        connection.close()


def run_suggest_related(db_path: str, moodle_path: str, file_path: str) -> dict:
    """Execute the ``suggest-related`` command."""

    connection = open_database(Path(db_path).expanduser().resolve())
    try:
        return success_payload(
            "suggest-related",
            suggest_related(connection, Path(moodle_path).expanduser().resolve(), file_path),
        )
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())

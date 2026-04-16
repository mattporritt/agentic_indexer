# Copyright (c) Moodle Pty Ltd. All rights reserved.
# Licensed under the Moodle Community License v1.3.
# See LICENSE.md in the repository root for full terms.
# Commercial use requires a separate written agreement with Moodle.

"""Stable runtime-facing contract builders for selected CLI commands.

This module adopts the canonical shared outer runtime schema used by
``agentic_devdocs`` and applies it to ``agentic_indexer`` results. The shared
schema governs the outer envelope, result shell, and provenance semantics while
tool-specific ``content`` payloads remain indexer-specific.
"""

from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from hashlib import sha1
from importlib.resources import files
from typing import Any


RUNTIME_TOOL_NAME = "agentic_indexer"
RUNTIME_VERSION = "v1"


@lru_cache(maxsize=1)
def _canonical_outer_schema() -> dict[str, Any]:
    """Load the vendored canonical shared outer runtime schema artifact."""

    schema_path = files("moodle_indexer").joinpath("contracts/runtime_outer_schema_v1.json")
    return json.loads(schema_path.read_text(encoding="utf-8"))


def normalize_contract_query(value: str | None) -> str:
    """Return a stable normalized form of a user query or lookup target."""

    return " ".join(str(value or "").strip().lower().split())


def build_runtime_contract(
    *,
    command: str,
    data: dict[str, Any],
    query: str,
    query_kind: str,
    limit: int,
    symbol_type: str | None = None,
    include_usages: bool | None = None,
) -> dict[str, Any]:
    """Wrap one supported command payload in the shared runtime contract."""

    if command == "find-definition":
        results = _definition_contract_results(data)
        intent = {
            "command": command,
            "query_kind": query_kind,
            "response_mode": "definition_lookup",
            "symbol_type_filter": symbol_type,
            "include_usages": include_usages,
            "limit": limit,
        }
    elif command == "semantic-context":
        results = _semantic_contract_results(data)
        intent = {
            "command": command,
            "query_kind": query_kind,
            "response_mode": "semantic_context",
            "symbol_type_filter": None,
            "include_usages": None,
            "limit": limit,
        }
    elif command == "build-context-bundle":
        results = _context_bundle_contract_results(data)
        intent = {
            "command": command,
            "query_kind": query_kind,
            "response_mode": "context_bundle",
            "symbol_type_filter": None,
            "include_usages": None,
            "limit": limit,
        }
    else:
        raise ValueError(f"Unsupported runtime contract command: {command}")

    payload = {
        "tool": RUNTIME_TOOL_NAME,
        "version": RUNTIME_VERSION,
        "query": query,
        "normalized_query": normalize_contract_query(query),
        "intent": intent,
        "results": results,
    }
    return validate_runtime_contract(payload)


def runtime_contract_schema() -> dict[str, Any]:
    """Return the vendored canonical shared outer runtime schema artifact."""

    return deepcopy(_canonical_outer_schema())


def validate_runtime_contract(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize one runtime contract envelope.

    The validation here is intentionally small and strict around the shared
    outer shape. Tool-specific ``content`` payloads remain flexible, but the
    runtime can rely on stable top-level/result/source semantics.
    """

    _require_mapping(payload, "runtime contract")
    schema = _canonical_outer_schema()
    allowed_confidence_values = set(schema["allowed_confidence_values"])
    _require_fields(payload, schema["required_top_level_fields"], context="runtime contract")

    if payload["tool"] != RUNTIME_TOOL_NAME:
        raise ValueError(f"Invalid runtime contract tool: {payload['tool']!r}")
    if payload["version"] != RUNTIME_VERSION:
        raise ValueError(f"Invalid runtime contract version: {payload['version']!r}")

    if not isinstance(payload["query"], str):
        raise ValueError("Runtime contract field 'query' must be a string.")
    if not isinstance(payload["normalized_query"], str):
        raise ValueError("Runtime contract field 'normalized_query' must be a string.")

    _require_mapping(payload["intent"], "runtime contract intent")
    if not isinstance(payload["results"], list):
        raise ValueError("Runtime contract field 'results' must be a list.")

    validated_results = [
        _validate_runtime_result(result, rank=index, schema=schema, allowed_confidence_values=allowed_confidence_values)
        for index, result in enumerate(payload["results"], start=1)
    ]

    return {
        "tool": payload["tool"],
        "version": payload["version"],
        "query": payload["query"],
        "normalized_query": payload["normalized_query"],
        "intent": payload["intent"],
        "results": validated_results,
    }


def _definition_contract_results(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return runtime-contract results for ``find-definition``."""

    results: list[dict[str, Any]] = []
    for rank, match in enumerate(list(data.get("matches", [])), start=1):
        path = _normalize_nullable_string(match.get("file"))
        fqname = str(match.get("fqname") or match.get("module_name") or match.get("name") or "")
        results.append(
            {
                "id": _stable_id("definition_match", path, fqname, str(match.get("line") or "")),
                "type": "definition_match",
                "rank": rank,
                "confidence": _normalize_confidence(_definition_confidence(match)),
                "source": _contract_source(path=path),
                "content": {
                    "symbol_type": match.get("symbol_type"),
                    "name": match.get("name"),
                    "fqname": match.get("fqname"),
                    "module_name": match.get("module_name"),
                    "component": match.get("component"),
                    "file": match.get("file"),
                    "line": match.get("line"),
                    "signature": match.get("signature"),
                    "docblock_summary": match.get("docblock_summary"),
                    "inheritance_role": match.get("inheritance_role"),
                    "parent_definition": _compact_definition_reference(match.get("parent_definition")),
                    "overrides_definition": _compact_definition_reference(match.get("overrides_definition")),
                    "implements_definitions": [
                        _compact_definition_reference(item) for item in list(match.get("implements_definitions", []))
                    ],
                    "usage_examples": [
                        {
                            "id": _stable_id(
                                "usage_example",
                                str(item.get("file") or ""),
                                str(item.get("line") or ""),
                                str(item.get("usage_kind") or ""),
                            ),
                            "file": item.get("file"),
                            "line": item.get("line"),
                            "usage_kind": item.get("usage_kind"),
                            "confidence": item.get("confidence"),
                            "snippet": item.get("snippet"),
                        }
                        for item in list(match.get("usage_examples", []))
                    ],
                },
                "diagnostics": {
                    "matched_via": match.get("matched_via"),
                    "usage_count": len(list(match.get("usage_examples", []))),
                    "selection_strategy": "definition_lookup",
                },
            }
        )
    return results


def _semantic_contract_results(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return runtime-contract results for ``semantic-context``."""

    items = list(data.get("primary_semantic_context", [])) + list(data.get("secondary_semantic_context", []))
    results: list[dict[str, Any]] = []
    for rank, item in enumerate(items, start=1):
        path = _normalize_nullable_string(item.get("path"))
        symbol = str(item.get("symbol") or "")
        results.append(
            {
                "id": _stable_id("semantic_context", str(item.get("chunk_id") or ""), path, symbol),
                "type": "semantic_context",
                "rank": rank,
                "confidence": _normalize_confidence(item.get("confidence")),
                "source": _contract_source(path=path),
                "content": {
                    "path": item.get("path"),
                    "symbol": item.get("symbol"),
                    "chunk_id": item.get("chunk_id"),
                    "result_kind": item.get("result_kind"),
                    "summary": item.get("summary"),
                    "snippet": item.get("snippet"),
                    "why_relevant_to_anchor": item.get("why_relevant_to_anchor"),
                },
                "diagnostics": {
                    "score": item.get("score"),
                    "retrieval_sources": list(item.get("retrieval_sources", [])),
                    "selection_strategy": "semantic_context",
                    "ranking_explanation": item.get("explanation"),
                },
            }
        )
    return results


def _context_bundle_contract_results(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return runtime-contract results for ``build-context-bundle``."""

    source_path = _bundle_source_path(data)
    query_kind = str(data.get("query_kind") or "")
    confidence = "high" if query_kind in {"symbol", "file"} else "medium"
    bundle_stats = dict(data.get("bundle_stats") or {})
    result = {
        "id": _stable_id("context_bundle", query_kind, source_path, str(data.get("query") or "")),
        "type": "context_bundle",
        "rank": 1,
        "confidence": _normalize_confidence(confidence),
        "source": _contract_source(path=source_path or None),
        "content": {
            "anchor": _bundle_anchor(data.get("anchor")),
            "primary_context": [_bundle_item_contract(item) for item in list(data.get("primary_context", []))],
            "supporting_context": [_bundle_item_contract(item) for item in list(data.get("supporting_context", []))],
            "optional_context": [_bundle_item_contract(item) for item in list(data.get("optional_context", []))],
            "tests_to_consider": [_bundle_item_contract(item) for item in list(data.get("tests_to_consider", []))],
            "guardrails": {
                "change_risk": dict(data.get("guardrails", {}).get("change_risk") or {}),
                "pre_edit_checks": [_bundle_nested_check(item) for item in list(data.get("guardrails", {}).get("pre_edit_checks", []))],
                "post_edit_checks": [_bundle_nested_check(item) for item in list(data.get("guardrails", {}).get("post_edit_checks", []))],
                "do_not_assume": [_bundle_nested_check(item) for item in list(data.get("guardrails", {}).get("do_not_assume", []))],
                "watch_points": [_bundle_nested_check(item) for item in list(data.get("guardrails", {}).get("watch_points", []))],
            },
            "example_patterns": [_bundle_item_contract(item) for item in list(data.get("example_patterns", []))],
            "recommended_reading_order": [
                {
                    "id": _stable_id("reading_step", str(item.get("step") or ""), str(item.get("target") or "")),
                    "step": item.get("step"),
                    "target": item.get("target"),
                    "why": item.get("why"),
                }
                for item in list(data.get("recommended_reading_order", []))
            ],
            "recommended_next_actions": list(data.get("recommended_next_actions", [])),
            "bundle_stats": bundle_stats,
            "notes": list(data.get("notes", [])),
        },
        "diagnostics": {
            "selection_strategy": "context_bundle",
            "primary_count": bundle_stats.get("primary_count", 0),
            "supporting_count": bundle_stats.get("supporting_count", 0),
            "optional_count": bundle_stats.get("optional_count", 0),
            "rough_token_estimate": bundle_stats.get("rough_token_estimate", 0),
        },
    }
    return [result]


def _bundle_anchor(anchor: Any) -> dict[str, Any] | None:
    """Return a compact stable anchor object for bundle content."""

    if not isinstance(anchor, dict):
        return None
    return {
        "path": anchor.get("path") or anchor.get("file"),
        "symbol": anchor.get("fqname") or anchor.get("symbol"),
        "component": anchor.get("component"),
        "file_role": anchor.get("file_role"),
        "symbol_type": anchor.get("symbol_type"),
        "line": anchor.get("line"),
    }


def _bundle_item_contract(item: dict[str, Any]) -> dict[str, Any]:
    """Return one stable nested context item for a context bundle."""

    path = str(item.get("path") or "")
    symbol = str(item.get("symbol") or "")
    return {
        "id": _stable_id("bundle_item", path, symbol, str(item.get("role") or "")),
        "path": item.get("path"),
        "symbol": item.get("symbol"),
        "role": item.get("role"),
        "confidence": item.get("confidence"),
        "reason": item.get("reason"),
        "summary": item.get("summary"),
        "snippet": item.get("snippet"),
    }


def _bundle_nested_check(item: dict[str, Any]) -> dict[str, Any]:
    """Return a stable nested guardrail or safety check entry."""

    path = str(item.get("path") or "")
    symbol = str(item.get("symbol") or "")
    reason = str(item.get("reason") or "")
    return {
        "id": _stable_id("bundle_check", path, symbol, reason),
        "path": item.get("path"),
        "symbol": item.get("symbol"),
        "confidence": item.get("confidence"),
        "reason": item.get("reason"),
    }


def _compact_definition_reference(value: Any) -> dict[str, Any] | None:
    """Return a small stable definition reference payload."""

    if not isinstance(value, dict):
        return None
    return {
        "fqname": value.get("fqname"),
        "file": value.get("file"),
        "line": value.get("line"),
        "symbol_type": value.get("symbol_type"),
    }


def _definition_confidence(match: dict[str, Any]) -> str:
    """Return a coarse contract confidence label for one definition result."""

    matched_via = str(match.get("matched_via") or "")
    if matched_via in {"direct_definition", "direct_module_name", "direct_fqname"}:
        return "high"
    if matched_via:
        return "medium"
    return "high"


def _bundle_source_path(data: dict[str, Any]) -> str:
    """Return the best source path for a context-bundle result."""

    anchor = data.get("anchor")
    if isinstance(anchor, dict):
        anchor_path = str(anchor.get("path") or anchor.get("file") or "")
        if anchor_path:
            return anchor_path
    for section_name in ("primary_context", "supporting_context", "optional_context"):
        items = list(data.get(section_name, []))
        if items:
            return str(items[0].get("path") or "")
    return ""


def _contract_source(*, path: str | None) -> dict[str, Any]:
    """Return a normalized provenance block for one contract result."""

    return {
        "name": "code_index",
        "type": "indexed_codebase",
        "url": None,
        "canonical_url": None,
        "path": _normalize_nullable_string(path),
        "document_title": None,
        "section_title": None,
        "heading_path": [],
    }


def _normalize_confidence(value: Any) -> str:
    """Return one of the shared coarse confidence labels."""

    allowed_confidence_values = set(_canonical_outer_schema()["allowed_confidence_values"])
    normalized = str(value or "medium").strip().lower()
    if normalized in allowed_confidence_values:
        return normalized
    return "medium"


def _normalize_nullable_string(value: Any) -> str | None:
    """Normalize empty/blank strings to ``None`` for stable nullable fields."""

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _require_mapping(value: Any, context: str) -> None:
    """Raise if a runtime contract section is not a mapping."""

    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object.")


def _require_fields(mapping: dict[str, Any], fields: list[str], *, context: str) -> None:
    """Raise if any required runtime contract fields are missing."""

    missing = [field for field in fields if field not in mapping]
    if missing:
        raise ValueError(f"{context} is missing required fields: {', '.join(missing)}")


def _validate_runtime_result(
    result: Any,
    *,
    rank: int,
    schema: dict[str, Any],
    allowed_confidence_values: set[str],
) -> dict[str, Any]:
    """Validate and normalize one runtime contract result."""

    _require_mapping(result, f"runtime contract result #{rank}")
    _require_fields(result, schema["required_result_fields"], context=f"runtime contract result #{rank}")
    _require_mapping(result["source"], f"runtime contract result #{rank} source")
    _require_fields(
        result["source"],
        schema["required_source_fields"],
        context=f"runtime contract result #{rank} source",
    )
    _require_mapping(result["content"], f"runtime contract result #{rank} content")
    _require_mapping(result["diagnostics"], f"runtime contract result #{rank} diagnostics")

    if not isinstance(result["id"], str) or not result["id"]:
        raise ValueError(f"runtime contract result #{rank} must include a non-empty string id")
    if not isinstance(result["type"], str) or not result["type"]:
        raise ValueError(f"runtime contract result #{rank} must include a non-empty string type")
    if not isinstance(result["rank"], int):
        raise ValueError(f"runtime contract result #{rank} must include an integer rank")
    if result["confidence"] not in allowed_confidence_values:
        raise ValueError(f"runtime contract result #{rank} has invalid confidence {result['confidence']!r}")
    if not isinstance(result["source"]["heading_path"], list):
        raise ValueError(f"runtime contract result #{rank} source heading_path must be a list")

    source = {
        "name": result["source"]["name"],
        "type": result["source"]["type"],
        "url": result["source"]["url"],
        "canonical_url": result["source"]["canonical_url"],
        "path": _normalize_nullable_string(result["source"]["path"]),
        "document_title": result["source"]["document_title"],
        "section_title": result["source"]["section_title"],
        "heading_path": list(result["source"]["heading_path"]),
    }

    return {
        "id": result["id"],
        "type": result["type"],
        "rank": result["rank"],
        "confidence": result["confidence"],
        "source": source,
        "content": result["content"],
        "diagnostics": result["diagnostics"],
    }


def _stable_id(*parts: str) -> str:
    """Return a deterministic stable identifier for contract entries."""

    normalized = "||".join(part.strip() for part in parts)
    return sha1(normalized.encode("utf-8")).hexdigest()[:16]

"""Bounded test-impact and execution-guardrail synthesis helpers.

This module holds the safety-specific planning logic that used to live inside
``queries.py``. Keeping it separate makes the agent-oriented safety layer
easier to reason about, test, and refactor without touching the rest of the
structural query code.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _normalize_php_symbol_name(name: str | None) -> str:
    """Return a normalized PHP symbol name for simple equality checks."""

    value = str(name or "").strip()
    while value.startswith("\\"):
        value = value[1:]
    value = value.replace("::class", "")
    return value


def _none_if_empty(value: object) -> object:
    """Return ``None`` for empty strings and pass other values through."""

    if isinstance(value, str) and not value.strip():
        return None
    return value


def _confidence_rank(confidence: str) -> int:
    """Return a deterministic ordering rank for confidence labels."""

    order = {"high": 0, "medium": 1, "low": 2}
    return order.get(confidence, 1)


def _component_root_for_path(path: str) -> str:
    """Return the component-root prefix for one Moodle path."""

    parts = [part for part in path.split("/") if part]
    if len(parts) >= 3 and parts[:2] == ["admin", "tool"]:
        return "/".join(parts[:3])
    if len(parts) >= 3 and parts[:2] == ["ai", "provider"]:
        return "/".join(parts[:3])
    if len(parts) >= 2 and parts[0] in {"mod", "block", "local", "theme", "enrol", "report", "question", "course", "admin"}:
        return "/".join(parts[:2])
    return parts[0] if parts else path


def _same_component_root(anchor: str, path: str) -> bool:
    """Return whether two Moodle paths appear to belong to the same component root."""

    return _component_root_for_path(anchor) == _component_root_for_path(path)


def _safety_item(
    *,
    reason: str,
    confidence: str = "high",
    path: str | None = None,
    symbol: str | None = None,
    priority: int = 50,
) -> dict[str, object]:
    """Return one bounded safety/test-impact item."""

    item: dict[str, object] = {
        "confidence": confidence,
        "reason": reason,
        "_priority": priority,
    }
    if path:
        item["path"] = path
    if symbol:
        item["symbol"] = symbol
    return item


def _dedupe_safety_items(items: list[dict[str, object]], *, limit: int) -> list[dict[str, object]]:
    """Return one bounded list of unique safety items.

    Safety sections are easier to scan when one file appears once with its
    strongest explanation instead of repeating under multiple near-identical
    messages. Path-based items therefore collapse by path, while no-path
    guidance collapses by reason text.
    """

    merged: dict[str, dict[str, object]] = {}
    for item in items:
        path = str(item.get("path") or "")
        reason = str(item.get("reason") or "")
        key = path or f"reason:{reason}"
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(item)
            continue
        item_priority = int(item.get("_priority", 50))
        existing_priority = int(existing.get("_priority", 50))
        item_confidence = _confidence_rank(str(item.get("confidence") or "medium"))
        existing_confidence = _confidence_rank(str(existing.get("confidence") or "medium"))
        if (item_priority, item_confidence, reason) < (
            existing_priority,
            existing_confidence,
            str(existing.get("reason") or ""),
        ):
            existing["reason"] = reason
            existing["_priority"] = item_priority
            existing["confidence"] = item.get("confidence", existing.get("confidence"))
        if not existing.get("symbol") and item.get("symbol"):
            existing["symbol"] = item["symbol"]
    ordered = sorted(
        merged.values(),
        key=lambda item: (
            int(item.get("_priority", 50)),
            _confidence_rank(str(item.get("confidence") or "medium")),
            str(item.get("path") or ""),
            str(item.get("reason") or ""),
        ),
    )
    cleaned: list[dict[str, object]] = []
    for item in ordered[:limit]:
        public_item = dict(item)
        public_item.pop("_priority", None)
        cleaned.append(public_item)
    return cleaned


def _test_file_path(path: str) -> bool:
    """Return whether one path looks like a concrete automated test."""

    return (
        "/tests/" in path
        or path.endswith("_test.php")
        or path.endswith("_advanced_testcase.php")
        or path.endswith(".feature")
    )


def _representative_service_pattern(connection: sqlite3.Connection) -> dict[str, object] | None:
    """Return one canonical external-service pattern with implementation, registration, and test."""

    rows = connection.execute(
        """
        SELECT
            w.service_name,
            w.classname,
            w.methodname,
            w.resolved_target_file,
            files.moodle_path AS service_path
        FROM webservices w
        JOIN files ON files.id = w.file_id
        WHERE w.resolution_status = 'resolved'
          AND w.resolved_target_file IS NOT NULL
          AND files.moodle_path LIKE '%/db/services.php'
        ORDER BY files.moodle_path, w.resolved_target_file, w.service_name
        """
    ).fetchall()

    best: dict[str, object] | None = None
    best_score = -1
    for row in rows:
        implementation_path = str(row["resolved_target_file"] or "")
        service_path = str(row["service_path"] or "")
        class_name = _normalize_php_symbol_name(str(row["classname"] or ""))
        method_name = str(row["methodname"] or "").strip()
        if not implementation_path or "::class" in implementation_path:
            continue
        component_root = _component_root_for_path(implementation_path)
        test_path = _representative_service_test_path(connection, implementation_path, component_root)
        if not test_path:
            continue

        score = 0
        if implementation_path.startswith("mod/"):
            score += 4
        elif implementation_path.startswith(("tool/", "admin/tool/")):
            score += 1
        if "/classes/external/" in implementation_path:
            score += 5
        elif implementation_path.endswith("/externallib.php"):
            score += 3
        if service_path.endswith("/db/services.php"):
            score += 3
        if test_path.endswith("_test.php"):
            score += 4
        if "/tests/external/" in test_path:
            score += 2

        symbol = _representative_service_symbol(
            connection,
            class_name=class_name,
            method_name=method_name,
            implementation_path=implementation_path,
        )
        candidate = {
            "service_name": str(row["service_name"] or ""),
            "service_path": service_path,
            "implementation_path": implementation_path,
            "implementation_symbol": symbol,
            "test_path": test_path,
            "component_root": component_root,
        }
        if score > best_score:
            best = candidate
            best_score = score

    return best


def _representative_service_test_path(
    connection: sqlite3.Connection,
    implementation_path: str,
    component_root: str,
) -> str | None:
    """Return one concrete representative PHPUnit path for an external service implementation."""

    stem = Path(implementation_path).stem
    candidates: list[str] = []
    if "/classes/external/" in implementation_path:
        candidates.append(f"{component_root}/tests/external/{stem}_test.php")
    if implementation_path.endswith("/externallib.php"):
        candidates.extend(
            [
                f"{component_root}/tests/externallib_test.php",
                f"{component_root}/tests/externallib_advanced_testcase.php",
            ]
        )
    for candidate in candidates:
        row = connection.execute(
            "SELECT 1 FROM files WHERE moodle_path = ? LIMIT 1",
            (candidate,),
        ).fetchone()
        if row is not None:
            return candidate
    return None


def _representative_service_symbol(
    connection: sqlite3.Connection,
    *,
    class_name: str,
    method_name: str,
    implementation_path: str,
) -> str | None:
    """Return one representative external method symbol when it can be resolved cheaply."""

    if class_name:
        preferred_names = [method_name] if method_name else []
        preferred_names.append("execute")
        for candidate_name in preferred_names:
            row = connection.execute(
                """
                SELECT symbols.fqname
                FROM symbols
                JOIN files ON files.id = symbols.file_id
                WHERE files.moodle_path = ?
                  AND symbols.symbol_type = 'method'
                  AND symbols.container_name = ?
                  AND symbols.name = ?
                ORDER BY symbols.line
                LIMIT 1
                """,
                (implementation_path, class_name, candidate_name),
            ).fetchone()
            if row is not None and row["fqname"]:
                return str(row["fqname"])
        row = connection.execute(
            """
            SELECT symbols.fqname
            FROM symbols
            JOIN files ON files.id = symbols.file_id
            WHERE files.moodle_path = ?
              AND symbols.symbol_type = 'method'
              AND symbols.container_name = ?
            ORDER BY CASE WHEN symbols.name = 'execute' THEN 0 ELSE 1 END, symbols.line
            LIMIT 1
            """,
            (implementation_path, class_name),
        ).fetchone()
        if row is not None and row["fqname"]:
            return str(row["fqname"])
    row = connection.execute(
        """
        SELECT symbols.fqname
        FROM symbols
        JOIN files ON files.id = symbols.file_id
        WHERE files.moodle_path = ?
          AND symbols.symbol_type = 'method'
        ORDER BY CASE WHEN symbols.name = 'execute' THEN 0 ELSE 1 END, symbols.line
        LIMIT 1
        """,
        (implementation_path,),
    ).fetchone()
    if row is not None and row["fqname"]:
        return str(row["fqname"])
    return None


def _plan_items(plan: dict[str, object]) -> list[dict[str, object]]:
    """Return all bounded change-plan items in one flat list."""

    items: list[dict[str, object]] = []
    for key in ("required_edits", "likely_edits", "optional_edits", "validation_impact"):
        items.extend(list(plan.get(key, [])))
    return items


def _collect_test_impact_tests(
    profile: dict[str, object],
    plan: dict[str, object],
    *,
    bucket: str,
    limit: int,
) -> list[dict[str, object]]:
    """Return direct or likely concrete tests from one bounded plan."""

    if bucket == "direct":
        sources = [plan.get("required_edits", [])]
    else:
        sources = [plan.get("validation_impact", []), plan.get("likely_edits", []), plan.get("optional_edits", [])]

    items: list[dict[str, object]] = []
    representative_pattern = dict(profile.get("representative_pattern") or {})
    if bucket == "direct" and bool(profile.get("service")) and str(profile.get("anchor_type") or "") == "query":
        test_path = str(representative_pattern.get("test_path") or "")
        if test_path:
            items.append(
                _safety_item(
                    path=test_path,
                    confidence="high",
                    reason="Representative PHPUnit coverage for the canonical Moodle external API change pattern; inspect and rerun it when parameters or service behavior change.",
                    priority=0,
                )
            )
    for source in sources:
        for item in source:
            path = str(item.get("path") or "")
            if not _test_file_path(path):
                continue
            confidence = str(item.get("confidence") or "medium")
            if bucket == "direct" and confidence != "high":
                continue
            items.append(
                _safety_item(
                    path=path,
                    symbol=_none_if_empty(item.get("symbol")),
                    confidence=confidence,
                    reason="Concrete automated coverage for this change lives here; review and rerun it if behavior, parameters, or output expectations change.",
                    priority=0 if bucket == "direct" else 10,
                )
            )
    return _dedupe_safety_items(items, limit=limit)


def _collect_environment_steps(
    profile: dict[str, object],
    plan: dict[str, object],
    *,
    limit: int,
) -> list[dict[str, object]]:
    """Return bounded non-test workflow steps implied by the change plan."""

    items: list[dict[str, object]] = []
    seen_paths: set[str] = set()
    if bool(profile.get("js")):
        for item in _plan_items(plan):
            path = str(item.get("path") or "")
            if "/amd/build/" not in path or path in seen_paths:
                continue
            items.append(
                _safety_item(
                    path=path,
                    symbol=_none_if_empty(item.get("symbol")),
                    confidence=str(item.get("confidence") or "medium"),
                    reason="Generated JavaScript artifact linked to the source module; rebuild or verify it if the workflow commits built assets.",
                )
            )
            seen_paths.add(path)
    return _dedupe_safety_items(items, limit=limit)


def _collect_contract_checks(
    profile: dict[str, object],
    plan: dict[str, object],
    *,
    limit: int,
) -> list[dict[str, object]]:
    """Return bounded contract or schema checks implied by the current slice."""

    items: list[dict[str, object]] = []
    anchor_label = str(profile.get("anchor_symbol") or profile.get("anchor_path") or "this change")
    anchor_type = str(profile.get("anchor_type") or "")
    representative_pattern = dict(profile.get("representative_pattern") or {})
    representative_service_added = False
    if bool(profile.get("service")) and anchor_type == "query":
        items.append(
            _safety_item(
                confidence="high",
                reason="Review the typical external API contract surface together: implementation parameters, service registration, and declared return shape often need coordinated updates.",
                priority=0,
            )
        )
        if representative_pattern.get("implementation_path"):
            items.append(
                _safety_item(
                    path=str(representative_pattern["implementation_path"]),
                    symbol=_none_if_empty(representative_pattern.get("implementation_symbol")),
                    confidence="high",
                    reason="Review parameter definitions and declared return structure on this representative external API method; signature changes usually require coordinated schema updates.",
                    priority=1,
                )
            )
        if representative_pattern.get("service_path"):
            items.append(
                _safety_item(
                    path=str(representative_pattern["service_path"]),
                    confidence="high",
                    reason="Review this representative web-service registration alongside the implementation; API parameter or return-shape changes often require entrypoint/schema updates here.",
                    priority=2,
                )
            )
            representative_service_added = True
    for item in _plan_items(plan):
        path = str(item.get("path") or "")
        if path.endswith("/db/services.php"):
            if bool(profile.get("service")) and anchor_type == "query" and representative_service_added:
                continue
            items.append(
                _safety_item(
                    path=path,
                    symbol=_none_if_empty(item.get("symbol")),
                    confidence=str(item.get("confidence") or "high"),
                    reason=f"Review the web-service registration for {anchor_label}; API parameter or return-shape changes often require entrypoint/schema updates here.",
                    priority=1 if bool(profile.get("service")) else 10,
                )
            )
            if bool(profile.get("service")) and anchor_type == "query":
                representative_service_added = True
        elif path.endswith("/renderer.php") and bool(profile.get("rendering")) and not bool(profile.get("service")):
            items.append(
                _safety_item(
                    path=path,
                    symbol=_none_if_empty(item.get("symbol")),
                    confidence="medium",
                    reason=f"Review renderer expectations for {anchor_label}; output-shape changes often require renderer-side consistency checks.",
                    priority=12,
                )
            )
        elif path.endswith(".mustache") and bool(profile.get("rendering")) and not bool(profile.get("service")):
            items.append(
                _safety_item(
                    path=path,
                    symbol=_none_if_empty(item.get("symbol")),
                    confidence="medium",
                    reason=f"Review template expectations for {anchor_label}; rendered context changes can require template updates or compatibility checks.",
                    priority=13,
                )
            )
        elif (path.endswith("/action_settings_form.php") or path.endswith("/action_form.php")) and bool(profile.get("provider_form")):
            items.append(
                _safety_item(
                    path=path,
                    symbol=_none_if_empty(item.get("symbol")),
                    confidence=str(item.get("confidence") or "medium"),
                    reason=f"Review form defaults and validation coupled to {anchor_label}; settings or field changes often need contract alignment here.",
                    priority=11,
                )
            )
    return _dedupe_safety_items(items, limit=limit)


def _collect_manual_review_points(
    profile: dict[str, object],
    plan: dict[str, object],
    *,
    limit: int,
) -> list[dict[str, object]]:
    """Return bounded manual review points that are not concrete tests."""

    items: list[dict[str, object]] = []
    anchor_label = str(profile.get("anchor_symbol") or profile.get("anchor_path") or "this change")
    if bool(profile.get("service")) and str(profile.get("anchor_type") or "") == "query":
        representative_pattern = dict(profile.get("representative_pattern") or {})
        if representative_pattern.get("implementation_path"):
            items.append(
                _safety_item(
                    path=str(representative_pattern["implementation_path"]),
                    symbol=_none_if_empty(representative_pattern.get("implementation_symbol")),
                    confidence="high",
                    reason=f"Use this representative external implementation as the canonical API-change pattern for {anchor_label}; it is the concrete code surface to compare before editing your real target.",
                    priority=0,
                )
            )
        else:
            representative_impl = next(
                (
                    item for item in plan.get("required_edits", [])
                    if str(item.get("change_role")) == "implementation"
                    and ("/classes/external/" in str(item.get("path") or "") or str(item.get("path") or "").endswith("/externallib.php"))
                ),
                None,
            )
            if representative_impl is not None:
                items.append(
                    _safety_item(
                        path=str(representative_impl["path"]),
                        symbol=_none_if_empty(representative_impl.get("symbol")),
                        confidence=str(representative_impl.get("confidence") or "high"),
                        reason=f"Use this representative external implementation as the canonical API-change pattern for {anchor_label}; it is the concrete code surface to compare before editing your real target.",
                        priority=0,
                    )
                )
    if bool(profile.get("service")):
        items.append(
            _safety_item(
                confidence="high",
                reason=f"Review backwards-compatibility expectations for {anchor_label}; external API signature and return-shape changes can affect callers beyond the direct PHPUnit coverage.",
                priority=1,
            )
        )
    if bool(profile.get("rendering")) and not bool(profile.get("service")):
        items.append(
            _safety_item(
                confidence="high",
                reason=f"Review renderer/template blast radius for {anchor_label}; large legacy rendering entrypoints can fan out into multiple output and template surfaces.",
                priority=1,
            )
        )
    if bool(profile.get("provider_form")):
        items.append(
            _safety_item(
                confidence="medium",
                reason=f"Review inherited provider/form behavior for {anchor_label}; field, default, or validation changes can drift from shared base classes.",
                priority=2,
            )
        )
    if bool(profile.get("js")):
        items.append(
            _safety_item(
                confidence="medium",
                reason=f"Review import and superclass expectations around {anchor_label}; client-side changes can regress dependent modules even when local edits are small.",
                priority=2,
            )
        )
    return _dedupe_safety_items(items, limit=limit)


def _classify_change_risk(
    profile: dict[str, object],
    plan: dict[str, object],
    test_impact: dict[str, object],
) -> dict[str, str]:
    """Return one conservative change-risk classification with explanation."""

    direct_tests = list(test_impact.get("direct_tests", []))
    anchor_path = str(profile.get("anchor_path") or "")
    anchor_type = str(profile.get("anchor_type") or "")
    if bool(profile.get("service")) and anchor_type == "query":
        return {
            "level": "high",
            "reason": "High risk because this free-text goal implies an external API contract change without one fixed implementation anchor; service registration and compatibility checks need extra care.",
        }
    if bool(profile.get("rendering")) and (anchor_path.endswith("/locallib.php") or anchor_path.endswith("/lib.php")):
        return {
            "level": "high",
            "reason": "High risk because this change touches a broad rendering-oriented legacy entrypoint with renderer/template companions and a wider blast radius.",
        }
    if bool(profile.get("service")):
        if direct_tests:
            return {
                "level": "medium",
                "reason": "Medium risk because external API and service-registration changes affect contracts, but concrete PHPUnit coverage and entrypoint checks are available.",
            }
        return {
            "level": "high",
            "reason": "High risk because this change affects an external API surface without strong direct automated coverage in the current bounded context.",
        }
    if bool(profile.get("provider_form")):
        return {
            "level": "medium",
            "reason": "Medium risk because provider settings changes can affect concrete forms and inherited base contracts even when the slice is structurally well-bounded.",
        }
    if bool(profile.get("js")):
        return {
            "level": "medium",
            "reason": "Medium risk because source-module changes can affect imports, superclass behavior, and generated build artifacts.",
        }
    return {
        "level": "low" if direct_tests else "medium",
        "reason": "Risk is bounded to a local implementation slice with the currently visible structural companions.",
    }


def _collect_pre_edit_checks(
    profile: dict[str, object],
    plan: dict[str, object],
    test_impact: dict[str, object],
    *,
    limit: int,
) -> list[dict[str, object]]:
    """Return bounded checks that should happen before editing."""

    items: list[dict[str, object]] = []
    required = list(plan.get("required_edits", []))
    likely = list(plan.get("likely_edits", []))
    representative_pattern = dict(profile.get("representative_pattern") or {})
    implementation = next((item for item in required if str(item.get("change_role")) == "implementation"), None)
    if bool(profile.get("service")) and str(profile.get("anchor_type") or "") == "query" and representative_pattern.get("implementation_path"):
        implementation = {
            "path": str(representative_pattern["implementation_path"]),
            "symbol": representative_pattern.get("implementation_symbol"),
            "confidence": "high",
        }
    if implementation is not None:
        items.append(
            _safety_item(
                path=str(implementation["path"]),
                symbol=_none_if_empty(implementation.get("symbol")),
                confidence=str(implementation.get("confidence") or "high"),
                reason="Inspect the defining implementation first so the change starts from the real behavior anchor rather than a companion artifact.",
                priority=0,
            )
        )
    if bool(profile.get("service")) and str(profile.get("anchor_type") or "") == "query":
        items.append(
            _safety_item(
                confidence="high",
                reason="Inspect one representative external implementation together with its paired PHPUnit coverage before editing so the change follows a canonical Moodle service pattern.",
                priority=0,
            )
        )
        if representative_pattern.get("service_path"):
            items.append(
                _safety_item(
                    path=str(representative_pattern["service_path"]),
                    confidence="high",
                    reason="Inspect this representative service registration before editing so API signature, declared parameters, and routing stay aligned with the implementation change.",
                    priority=1,
                )
            )
    direct_test = next(iter(test_impact.get("direct_tests", [])), None)
    if direct_test is not None:
        items.append(
            _safety_item(
                path=_none_if_empty(direct_test.get("path")),
                symbol=_none_if_empty(direct_test.get("symbol")),
                confidence=str(direct_test.get("confidence") or "high"),
                reason="Inspect the most direct automated coverage before editing so expected behavior and assertions are clear.",
                priority=2,
            )
        )
    if bool(profile.get("service")):
        entrypoint = next((item for item in required + likely if str(item.get("change_role")) == "entrypoint"), None)
        if entrypoint is not None:
            items.append(
                _safety_item(
                    path=str(entrypoint["path"]),
                    symbol=_none_if_empty(entrypoint.get("symbol")),
                    confidence=str(entrypoint.get("confidence") or "high"),
                    reason="Inspect service registration alongside the implementation before changing parameters or return structures.",
                    priority=1,
                )
            )
    if bool(profile.get("rendering")) and not bool(profile.get("service")):
        usage_files = list(profile.get("usage_files") or [])
        usage_file = next((path for path in usage_files if _same_component_root(str(profile.get("anchor_path") or ""), path)), None)
        if usage_file is not None:
            items.append(
                _safety_item(
                    path=usage_file,
                    confidence="high",
                    reason="Inspect the closest component-local usage next so you can see how this legacy method is invoked before following the broader rendering chain.",
                    priority=1,
                )
            )
        companion = next((item for item in required + likely if str(item.get("change_role")) == "rendering_companion"), None)
        if companion is not None:
            items.append(
                _safety_item(
                    path=str(companion["path"]),
                    symbol=_none_if_empty(companion.get("symbol")),
                    confidence=str(companion.get("confidence") or "medium"),
                    reason="Inspect the direct rendering companion before editing so output-shape changes stay aligned with renderer/template expectations.",
                    priority=2,
                )
            )
    if bool(profile.get("provider_form")):
        companion = next((item for item in required + likely if str(item.get("change_role")) == "form_companion"), None)
        if companion is not None:
            items.append(
                _safety_item(
                    path=str(companion["path"]),
                    symbol=_none_if_empty(companion.get("symbol")),
                    confidence=str(companion.get("confidence") or "medium"),
                    reason="Inspect the concrete form chain before editing so field and validation expectations stay aligned with the provider method.",
                    priority=1,
                )
            )
    if bool(profile.get("js")):
        companion = next((item for item in required + likely if str(item.get("path", "")).startswith("lib/amd/") or "/amd/src/" in str(item.get("path", ""))), None)
        if companion is not None and str(companion.get("path")) != str(implementation.get("path") if implementation else ""):
            items.append(
                _safety_item(
                    path=str(companion["path"]),
                    symbol=_none_if_empty(companion.get("symbol")),
                    confidence=str(companion.get("confidence") or "medium"),
                    reason="Inspect imported or inherited module behavior before editing the source module so public client-side contracts stay intact.",
                    priority=1,
                )
            )
    return _dedupe_safety_items(items, limit=limit)


def _collect_post_edit_checks(
    profile: dict[str, object],
    plan: dict[str, object],
    test_impact: dict[str, object],
    *,
    limit: int,
) -> list[dict[str, object]]:
    """Return bounded checks that should happen after editing."""

    items: list[dict[str, object]] = []
    items.extend(list(test_impact.get("direct_tests", [])))
    items.extend(list(test_impact.get("environment_steps", [])))
    if bool(profile.get("service")):
        for item in test_impact.get("contract_checks", []):
            path = str(item.get("path") or "")
            if path.endswith("/db/services.php"):
                items.append(
                    _safety_item(
                        path=path,
                        symbol=_none_if_empty(item.get("symbol")),
                        confidence=str(item.get("confidence") or "high"),
                        reason="Recheck the service registration after editing to confirm parameter, return-schema, and routing consistency.",
                    )
                )
                break
    if bool(profile.get("rendering")):
        items.append(
            _safety_item(
                confidence="medium",
                reason="Review output, renderer, and template consistency after the edit so rendered structure changes stay aligned end to end.",
            )
        )
    return _dedupe_safety_items(items, limit=limit)


def _collect_do_not_assume(
    profile: dict[str, object],
    plan: dict[str, object],
    *,
    limit: int,
) -> list[dict[str, object]]:
    """Return bounded assumptions an agent should avoid making."""

    items: list[dict[str, object]] = []
    if bool(profile.get("service")):
        items.append(_safety_item(confidence="high", reason="Do not assume service registration updates itself when external API parameters or return structures change."))
        items.append(_safety_item(confidence="medium", reason="Do not assume one direct PHPUnit file covers every external API compatibility edge around this service."))
    if bool(profile.get("rendering")) and not bool(profile.get("service")):
        items.append(_safety_item(confidence="high", reason="Do not assume template impact is absent just because the change starts in PHP; rendering contracts often span output classes, renderers, and Mustache templates."))
    if bool(profile.get("provider_form")):
        items.append(_safety_item(confidence="medium", reason="Do not assume shared form bases inherit provider-field changes safely without reviewing defaults and validation rules."))
    if bool(profile.get("js")):
        items.append(_safety_item(confidence="medium", reason="Do not assume generated AMD build artifacts or dependent imports stay correct without verification after source changes."))
    return _dedupe_safety_items(items, limit=limit)


def _collect_watch_points(
    profile: dict[str, object],
    plan: dict[str, object],
    *,
    limit: int,
) -> list[dict[str, object]]:
    """Return bounded watch-points that deserve extra care while editing."""

    items: list[dict[str, object]] = []
    if bool(profile.get("service")):
        items.append(_safety_item(confidence="high", reason="Watch backwards-compatibility and consumer expectations if external API parameters, warnings, or return structures change."))
    if bool(profile.get("rendering")) and not bool(profile.get("service")):
        items.append(_safety_item(confidence="high", reason="Watch the blast radius of large legacy rendering entrypoints; related output classes and templates may need coordinated updates."))
    if bool(profile.get("provider_form")):
        items.append(_safety_item(confidence="medium", reason="Watch for drift between concrete provider forms, shared form bases, and provider contract methods."))
    if bool(profile.get("js")):
        items.append(_safety_item(confidence="medium", reason="Watch import and superclass regressions, especially when the workflow commits generated AMD build artifacts."))
    return _dedupe_safety_items(items, limit=limit)


def _synthesize_test_impact(
    *,
    query: str,
    query_kind: str,
    anchor: dict[str, object] | None,
    profile: dict[str, object],
    plan: dict[str, object],
    limit: int,
) -> dict[str, object]:
    """Translate one bounded change plan into a bounded validation view.

    The synthesis preserves a strict separation between concrete tests, broader
    likely tests, environment/build steps, contract checks, and manual review
    guidance. Later guardrail logic reuses these sections directly, so keeping
    this shaping predictable is important for both explainability and safe
    refactoring.
    """

    direct_tests = _collect_test_impact_tests(profile, plan, bucket="direct", limit=min(limit, 4))
    likely_tests = _collect_test_impact_tests(profile, plan, bucket="likely", limit=min(limit, 4))
    direct_paths = {str(item.get("path") or "") for item in direct_tests if item.get("path")}
    likely_tests = [
        item for item in likely_tests
        if str(item.get("path") or "") not in direct_paths
    ][: min(limit, 4)]
    environment_steps = _collect_environment_steps(profile, plan, limit=min(limit, 4))
    contract_checks = _collect_contract_checks(profile, plan, limit=min(limit, 4))
    manual_review_points = _collect_manual_review_points(profile, plan, limit=min(limit, 4))

    return {
        "query": query,
        "query_kind": query_kind,
        "anchor": anchor,
        "direct_tests": direct_tests,
        "likely_tests": likely_tests,
        "environment_steps": environment_steps,
        "contract_checks": contract_checks,
        "manual_review_points": manual_review_points,
        "notes": [
            "This test-impact view is bounded and conservative. It prioritizes concrete tests and contract checks over generic validation advice.",
        ],
    }


def _synthesize_execution_guardrails(
    *,
    query: str,
    query_kind: str,
    anchor: dict[str, object] | None,
    profile: dict[str, object],
    plan: dict[str, object],
    test_impact: dict[str, object],
    limit: int,
) -> dict[str, object]:
    """Translate one bounded plan and validation view into execution guardrails.

    Guardrails are deliberately short and operational: classify the overall
    risk, show the highest-value pre-edit and post-edit checks, and capture
    only the most important assumptions and watch-points.
    """

    risk = _classify_change_risk(profile, plan, test_impact)
    pre_edit_checks = _collect_pre_edit_checks(profile, plan, test_impact, limit=min(limit, 5))
    post_edit_checks = _collect_post_edit_checks(profile, plan, test_impact, limit=min(limit, 5))
    do_not_assume = _collect_do_not_assume(profile, plan, limit=min(limit, 4))
    watch_points = _collect_watch_points(profile, plan, limit=min(limit, 4))

    return {
        "query": query,
        "query_kind": query_kind,
        "anchor": anchor,
        "change_risk": risk,
        "pre_edit_checks": pre_edit_checks,
        "post_edit_checks": post_edit_checks,
        "do_not_assume": do_not_assume,
        "watch_points": watch_points,
        "notes": [
            "These guardrails are intentionally short and conservative. They highlight the strongest local risks and checks before finalizing edits.",
        ],
    }

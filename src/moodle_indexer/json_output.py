"""JSON serialization helpers for deterministic CLI responses."""

from __future__ import annotations

import json
from typing import Any


def dumps_json(payload: dict[str, Any]) -> str:
    """Serialize payloads in a deterministic, machine-friendly format."""

    return json.dumps(payload, indent=2, sort_keys=True)


def success_payload(command: str, data: dict[str, Any]) -> dict[str, Any]:
    """Wrap successful command output in a consistent envelope."""

    return {"command": command, "status": "ok", "data": data}


def error_payload(command: str, message: str, error_type: str = "error") -> dict[str, Any]:
    """Wrap errors in a consistent envelope."""

    return {"command": command, "status": "error", "error": {"type": error_type, "message": message}}

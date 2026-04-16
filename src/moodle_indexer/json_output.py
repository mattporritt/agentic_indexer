# Copyright (c) Moodle Pty Ltd. All rights reserved.
# Licensed under the Moodle Community License v1.3.
# See LICENSE.md in the repository root for full terms.
# Commercial use requires a separate written agreement with Moodle.

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

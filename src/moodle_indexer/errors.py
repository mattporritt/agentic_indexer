# Copyright (c) Moodle Pty Ltd. All rights reserved.
# Licensed under the Moodle Community License v1.3.
# See LICENSE.md in the repository root for full terms.
# Commercial use requires a separate written agreement with Moodle.

"""Custom exception types for clear, structured CLI failures."""


class IndexerError(Exception):
    """Base exception for user-facing indexer failures."""


class ValidationError(IndexerError):
    """Raised when user input or paths are invalid."""

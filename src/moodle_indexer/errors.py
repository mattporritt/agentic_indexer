"""Custom exception types for clear, structured CLI failures."""


class IndexerError(Exception):
    """Base exception for user-facing indexer failures."""


class ValidationError(IndexerError):
    """Raised when user input or paths are invalid."""

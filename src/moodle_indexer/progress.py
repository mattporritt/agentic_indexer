"""Minimal progress reporting for long-running CLI operations.

The index command emits JSON on stdout, so progress reporting lives on stderr.
This module provides small dependency-free progress helpers that work both in
TTY sessions and in captured test output.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class ProgressBar:
    """Render simple phase progress updates for index builds."""

    total: int
    label: str = "Indexing files"
    width: int = 28
    _current: int = field(init=False, repr=False)
    _last_percent: int = field(init=False, repr=False)
    _last_reported_count: int = field(init=False, repr=False)
    _is_tty: bool = field(init=False, repr=False)
    _closed: bool = field(init=False, repr=False)
    _start_time: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._current = 0
        self._last_percent = -1
        self._last_reported_count = -1
        self._is_tty = sys.stderr.isatty()
        self._closed = False
        self._start_time = time.perf_counter()
        self._render(force=True)

    def advance(self, step: int = 1) -> None:
        """Advance the progress bar by a fixed step."""

        self._current = min(self.total, self._current + step)
        self._render()

    def close(self) -> None:
        """Finalize the progress bar output."""

        if self._closed:
            return
        self._current = self.total
        self._render(force=True)
        sys.stderr.write("\n")
        sys.stderr.flush()
        self._closed = True

    def _render(self, force: bool = False) -> None:
        total = max(self.total, 1)
        percent = int((self._current / total) * 100)
        if not force and not self._should_render(percent):
            return
        self._last_percent = percent
        self._last_reported_count = self._current

        filled = int((self._current / total) * self.width)
        bar = "#" * filled + "-" * (self.width - filled)
        elapsed = time.perf_counter() - self._start_time
        rate = self._current / elapsed if elapsed > 0 else 0.0
        line = (
            f"{self.label} [{bar}] {self._current}/{self.total} "
            f"({percent:3d}%) {rate:0.1f} files/s"
        )

        if self._is_tty:
            sys.stderr.write(f"\r{line}")
        else:
            sys.stderr.write(f"{line}\n")
        sys.stderr.flush()

    def _should_render(self, percent: int) -> bool:
        """Return whether the latest state should be printed."""

        if self._is_tty:
            return True
        if percent != self._last_percent:
            return True
        step = max(1, self.total // 20)
        return (self._current // step) != (self._last_reported_count // step)

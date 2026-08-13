"""Shared machine-readable workflow errors."""

from __future__ import annotations


class WorkflowError(RuntimeError):
    """A bounded workflow failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str, category: str = "FAILED") -> None:
        super().__init__(message)
        self.code = code
        self.category = category

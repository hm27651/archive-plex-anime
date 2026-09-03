"""Shared machine-readable workflow errors."""

from __future__ import annotations

from typing import Any


class WorkflowError(RuntimeError):
    """A bounded workflow failure with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        category: str = "FAILED",
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.category = category
        self.details = details or {}
        self.retryable = retryable

"""Stable, structured errors returned by Raven tools."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    FILE_IN_USE = "FILE_IN_USE"
    UNSUPPORTED_DOCUMENT = "UNSUPPORTED_DOCUMENT"
    UNSAFE_PACKAGE = "UNSAFE_PACKAGE"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    STALE_REVISION = "STALE_REVISION"
    ANCHOR_NOT_FOUND = "ANCHOR_NOT_FOUND"
    ANCHOR_AMBIGUOUS = "ANCHOR_AMBIGUOUS"
    PROTECTED_BOUNDARY = "PROTECTED_BOUNDARY"
    UNSUPPORTED_REVISION = "UNSUPPORTED_REVISION"
    REFERENCE_UNRESOLVED = "REFERENCE_UNRESOLVED"
    ZOTERO_UNAVAILABLE = "ZOTERO_UNAVAILABLE"
    ZOTERO_INCOMPATIBLE = "ZOTERO_INCOMPATIBLE"
    TRANSACTION_NOT_FOUND = "TRANSACTION_NOT_FOUND"
    COMMIT_CONFLICT = "COMMIT_CONFLICT"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(slots=True)
class RavenError(Exception):
    """An expected failure with machine-readable remediation details."""

    code: ErrorCode
    message: str
    stage: str
    retryable: bool = False
    remediation: str | None = None
    locator: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "stage": self.stage,
            "retryable": self.retryable,
            "remediation": self.remediation,
            "locator": self.locator,
        }

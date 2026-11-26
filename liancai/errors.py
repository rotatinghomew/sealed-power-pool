"""Error types shared by the service, CLI and HTTP layer.

Every error carries a stable machine readable ``code`` and an HTTP status so
that the JSON API can return useful, non-leaking messages without either
layer having to guess.
"""

from __future__ import annotations


class AppError(Exception):
    code = "ERROR"
    http_status = 400

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def as_dict(self) -> dict:
        body = {"error": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return body


class ValidationError(AppError):
    code = "VALIDATION_ERROR"
    http_status = 422


class NotFound(AppError):
    code = "NOT_FOUND"
    http_status = 404


class Forbidden(AppError):
    code = "FORBIDDEN"
    http_status = 403


class Sealed(AppError):
    """The requested material is still sealed by the tender timetable."""

    code = "SEALED"
    http_status = 423


class Conflict(AppError):
    code = "CONFLICT"
    http_status = 409


class Expired(AppError):
    code = "EXPIRED"
    http_status = 409


class EvaluationTooComplex(AppError):
    code = "EVALUATION_TOO_COMPLEX"
    http_status = 422

from fastapi import HTTPException, Request, status

from .models import ErrorDetail


class ApiError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str, details=None):
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.message = message
        self.details = details or []


def error_body(code: str, message: str, request_id: str, details=None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or [],
            "request_id": request_id,
        }
    }


def validation_failed(details: list[ErrorDetail]):
    return ApiError(422, "VALIDATION_FAILED", "Request body validation failed",
                    [d.model_dump() for d in details])


def transition_invalid(msg: str):
    return ApiError(409, "TRANSITION_INVALID", msg)


def not_found(msg: str = "Resource not found"):
    return ApiError(404, "NOT_FOUND", msg)


def forbidden(msg: str = "Not authorized for this operation"):
    return ApiError(403, "FORBIDDEN", msg)


def already_decided(msg: str = "Decision already recorded"):
    return ApiError(409, "ALREADY_DECIDED", msg)

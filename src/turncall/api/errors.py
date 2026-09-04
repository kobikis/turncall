"""Error handling framework with structured API error responses."""

from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorCode(StrEnum):
    """Application error codes."""

    # General
    INTERNAL_ERROR = "internal_error"
    NOT_FOUND = "not_found"
    VALIDATION_ERROR = "validation_error"
    CONFLICT = "conflict"

    # Auth
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    INVALID_API_KEY = "invalid_api_key"
    REVOKED_API_KEY = "revoked_api_key"

    # Agent
    AGENT_NOT_FOUND = "agent_not_found"
    AGENT_ALREADY_PUBLISHED = "agent_already_published"
    INVALID_AGENT_CONFIG = "invalid_agent_config"

    # Call
    CALL_NOT_FOUND = "call_not_found"
    CALL_NOT_ACTIVE = "call_not_active"
    INVALID_STATE_TRANSITION = "invalid_state_transition"

    # Phone Number
    PHONE_NUMBER_NOT_FOUND = "phone_number_not_found"
    PHONE_NUMBER_ALREADY_BOUND = "phone_number_already_bound"

    # Tool
    TOOL_NOT_FOUND = "tool_not_found"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    TOOL_TIMEOUT = "tool_timeout"

    # Twilio
    TWILIO_ERROR = "twilio_error"
    TWILIO_SIGNATURE_INVALID = "twilio_signature_invalid"

    # Rate Limiting
    RATE_LIMITED = "rate_limited"
    CONCURRENCY_LIMIT = "concurrency_limit"


class ErrorResponse(BaseModel):
    """Structured API error response."""

    model_config = ConfigDict(frozen=True)

    success: bool = False
    error: str
    code: ErrorCode
    details: dict[str, Any] | None = None
    request_id: str | None = None


class ApiError(Exception):
    """Base API exception."""

    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


class BadRequestError(ApiError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            status_code=400,
            code=ErrorCode.VALIDATION_ERROR,
            message=message,
            details=details,
        )


class NotFoundError(ApiError):
    def __init__(self, resource: str, resource_id: str) -> None:
        super().__init__(
            status_code=404,
            code=ErrorCode.NOT_FOUND,
            message=f"{resource} not found: {resource_id}",
            details={"resource": resource, "id": resource_id},
        )


class ConflictError(ApiError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            status_code=409,
            code=ErrorCode.CONFLICT,
            message=message,
            details=details,
        )


class UnauthorizedError(ApiError):
    def __init__(self, message: str = "Invalid or missing API key") -> None:
        super().__init__(
            status_code=401,
            code=ErrorCode.UNAUTHORIZED,
            message=message,
        )


class ForbiddenError(ApiError):
    def __init__(self, message: str = "Insufficient permissions") -> None:
        super().__init__(
            status_code=403,
            code=ErrorCode.FORBIDDEN,
            message=message,
        )


class InvalidStateTransitionError(ApiError):
    def __init__(self, current: str, target: str) -> None:
        super().__init__(
            status_code=409,
            code=ErrorCode.INVALID_STATE_TRANSITION,
            message=f"Cannot transition from {current} to {target}",
            details={"current_state": current, "target_state": target},
        )


def register_error_handlers(app: FastAPI) -> None:
    """Register exception handlers on the FastAPI app."""

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                error=exc.message,
                code=exc.code,
                details=exc.details,
                request_id=getattr(request.state, "request_id", None),
            ).model_dump(),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # jsonable_encoder makes the error list JSON-safe: a model_validator that
        # raises ValueError (e.g. an invalid s2s voice) puts the raw exception in
        # each error's `ctx`, which json.dumps can't serialize — without this the
        # 422 handler itself raised and the response became a 500.
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                error="Validation error",
                code=ErrorCode.VALIDATION_ERROR,
                details={"errors": jsonable_encoder(exc.errors())},
                request_id=getattr(request.state, "request_id", None),
            ).model_dump(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                error=str(exc.detail),
                code=ErrorCode.INTERNAL_ERROR,
                request_id=getattr(request.state, "request_id", None),
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        from loguru import logger

        logger.error(
            "Unhandled exception: {exc_type} - {exc_msg}",
            exc_type=type(exc).__name__,
            exc_msg=str(exc),
            request_id=getattr(request.state, "request_id", None),
        )
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="Internal server error",
                code=ErrorCode.INTERNAL_ERROR,
                request_id=getattr(request.state, "request_id", None),
            ).model_dump(),
        )

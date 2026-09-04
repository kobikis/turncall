"""Standardized API response envelope."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class ApiResponse[T](BaseModel):
    """Standard API response wrapper."""

    model_config = ConfigDict(frozen=True)

    success: bool = True
    data: T | None = None
    error: str | None = None
    metadata: dict[str, Any] | None = None


class PaginatedResponse[T](BaseModel):
    """Paginated API response wrapper."""

    model_config = ConfigDict(frozen=True)

    success: bool = True
    data: list[T]
    total: int
    page: int
    limit: int
    error: str | None = None


def ok(data: Any, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Shorthand for a success response."""
    response: dict[str, Any] = {"success": True, "data": data}
    if metadata is not None:
        response["metadata"] = metadata
    return response


def paginated(
    data: list[Any],
    total: int,
    page: int,
    limit: int,
) -> dict[str, Any]:
    """Shorthand for a paginated success response."""
    return {
        "success": True,
        "data": data,
        "total": total,
        "page": page,
        "limit": limit,
    }

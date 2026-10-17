"""The audit trail a route records its changes on (#317, ADR-0024).

A route that changes an agent, a project credential, a phone number, an API
key or a project setting takes `Audit` (or `PlatformAudit`, on the bootstrap
routes the platform credential calls) and records each change with
`await audit.record(...)`, before any commit of its own. Three things hold the
rest:

- **Every such route takes one.** `tests/unit/test_audit_wiring.py` lists
  every mutating route under `AUDITED_PREFIXES` and fails on one without it.
- **A successful request records.** The trail's teardown runs before the
  session commits (both are function-scoped), and raises if the request
  succeeded with no entry: the change is rolled back, and the client gets a
  500, rather than a change committed unrecorded.
- **The ids reach the client.** Each entry's id is put on the response as
  `X-TurnCall-Audit-Id` (comma-separated when one request wrote several), so
  the builder can link its own entry to the engine's.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Request, Response
from fastapi.routing import APIRoute, iter_route_contexts

from turncall.api.deps import DbSession
from turncall.auth.dependencies import Auth
from turncall.domain.enums import AuditActor
from turncall.services.audit_log import AuditTrail

AUDIT_HEADER = "X-TurnCall-Audit-Id"

# Every mutating route under these is audited (the wiring test holds it).
AUDITED_PREFIXES = (
    "/v1/agents",
    "/v1/credentials",
    "/v1/phone-numbers",
    "/v1/api-keys",
    "/v1/projects",
)
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _trail(
    session: Any, request: Request, response: Response, **who: Any
) -> AuditTrail:
    def on_record(ids: list[UUID]) -> None:
        response.headers[AUDIT_HEADER] = ", ".join(str(i) for i in ids)

    # The client may send its own X-Request-ID; the column holds 128.
    request_id = getattr(request.state, "request_id", None)
    return AuditTrail(
        session=session,
        request_id=request_id[:128] if request_id else None,
        on_record=on_record,
        **who,
    )


async def api_key_trail(
    auth: Auth, session: DbSession, request: Request, response: Response
) -> AsyncIterator[AuditTrail]:
    """The trail of a request made with a project's API key."""
    trail = _trail(
        session,
        request,
        response,
        actor=AuditActor.API_KEY,
        api_key_id=auth.api_key_id,
        project_id=auth.project_id,
    )
    yield trail
    trail.require_entry(f"{request.method} {request.url.path}")


async def platform_trail(
    session: DbSession, request: Request, response: Response
) -> AsyncIterator[AuditTrail]:
    """The trail of a bootstrap request, made with the platform credential:
    no API key, and the project is the one it creates or keys."""
    trail = _trail(
        session,
        request,
        response,
        actor=AuditActor.PLATFORM,
        api_key_id=None,
        project_id=None,
    )
    yield trail
    trail.require_entry(f"{request.method} {request.url.path}")


# scope="function", like DbSession's: the teardown's check runs before the
# commit, so a request that recorded nothing commits nothing.
Audit = Annotated[AuditTrail, Depends(api_key_trail, scope="function")]
PlatformAudit = Annotated[AuditTrail, Depends(platform_trail, scope="function")]

TRAILS = (api_key_trail, platform_trail)


def api_routes(app: FastAPI) -> Iterator[tuple[str, APIRoute]]:
    """Every API route the app serves, with its full path."""
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if isinstance(route, APIRoute) and context.path:
            yield context.path, route


def uses_audit(route: APIRoute) -> bool:
    """Whether the route takes an audit trail, at any depth."""
    pending = [route.dependant]
    while pending:
        dependant = pending.pop()
        if dependant.call in TRAILS:
            return True
        pending.extend(dependant.dependencies)
    return False


HEADER_DOC = {
    "description": (
        "The id of each audit log entry this request wrote, comma-separated, "
        "in order (`GET /v1/audit-log` returns them)."
    ),
    "schema": {"type": "string"},
}


def document_audit_header(app: FastAPI) -> None:
    """Name `X-TurnCall-Audit-Id` on each audited route's success response
    in the OpenAPI spec, as it is built."""
    build = app.openapi

    def openapi() -> dict[str, Any]:
        spec = build()  # cached by FastAPI: this edits the cached spec
        for path, route in api_routes(app):
            if not uses_audit(route):
                continue
            status = str(route.status_code or 200)
            for method in route.methods or ():
                operation = spec["paths"].get(path, {}).get(method.lower())
                if operation is None:
                    continue
                response = operation["responses"].setdefault(status, {})
                response.setdefault("headers", {})[AUDIT_HEADER] = HEADER_DOC
        return spec

    app.openapi = openapi  # type: ignore[method-assign]

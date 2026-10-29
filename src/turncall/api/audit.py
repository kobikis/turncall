"""The audit trail a route records its changes on (#317, ADR-0024).

A route that changes an agent, a skill, a project credential, a phone number,
an API key or a project setting takes `Audit` (or `PlatformAudit`, on the
bootstrap routes the platform credential calls) and records each change: `with
audit.change(...)` for a resource that existed, which takes its "before"
itself, or `audit.record(...)` for one it created. Three things hold the rest:

- **Every such route takes one.** `tests/unit/test_audit_wiring.py` lists
  every mutating route under `AUDITED_PREFIXES` and fails on one without it.
- **A committed request recorded.** The trail is bound to the request's
  session, whose `before_commit` writes its entries and refuses the commit if
  it recorded nothing: the change is rolled back and the client gets a 500,
  rather than a change saved unrecorded. Whoever commits, DbSession at the
  end or the route itself before a side effect, the check is the commit's.
- **The client can find them.** The request's first entry, the one for its
  primary resource, is named on the response as `X-TurnCall-Audit-Id`; every
  entry the request wrote carries that id as its `request_id`, so
  `GET /v1/audit-log?request_id=` returns the whole request.
"""

from __future__ import annotations

from collections.abc import Iterator
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
    "/v1/skills",
    "/v1/credentials",
    "/v1/phone-numbers",
    "/v1/api-keys",
    "/v1/projects",
)
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _trail(
    session: Any, request: Request, response: Response, **who: Any
) -> AuditTrail:
    # Named when the first entry is recorded: FastAPI copies the injected
    # response's headers onto the real one before a dependency's teardown
    # runs, so a teardown is too late to set one.
    def name(entry_id: UUID) -> None:
        response.headers[AUDIT_HEADER] = str(entry_id)

    return AuditTrail.bind(
        session, where=f"{request.method} {request.url.path}", on_first=name, **who
    )


def api_key_trail(
    auth: Auth, session: DbSession, request: Request, response: Response
) -> AuditTrail:
    """The trail of a request made with a project's API key."""
    return _trail(
        session,
        request,
        response,
        actor=AuditActor.API_KEY,
        api_key_id=auth.api_key_id,
        project_id=auth.project_id,
    )


def platform_trail(
    session: DbSession, request: Request, response: Response
) -> AuditTrail:
    """The trail of a bootstrap request, made with the platform credential:
    no API key, and the project is the one it creates or keys."""
    return _trail(
        session,
        request,
        response,
        actor=AuditActor.PLATFORM,
        api_key_id=None,
        project_id=None,
    )


Audit = Annotated[AuditTrail, Depends(api_key_trail)]
PlatformAudit = Annotated[AuditTrail, Depends(platform_trail)]

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
        "The id of the audit log entry for this request's primary resource. "
        "Every entry the request wrote (a publish also archives versions and "
        "re-points numbers) carries it as `request_id`: "
        "`GET /v1/audit-log?request_id=<this id>` returns them all."
    ),
    "schema": {"type": "string", "format": "uuid"},
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

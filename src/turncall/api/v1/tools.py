"""Tool registration and invocation query endpoints."""

from uuid import UUID

from fastapi import APIRouter

from turncall.api.deps import DbSession
from turncall.api.errors import NotFoundError
from turncall.api.responses import ok
from turncall.api.v1.schemas.tools import (
    RegisterToolRequest,
    ToolInvocationResponse,
    ToolResponse,
)
from turncall.auth import Auth, WriteAuth
from turncall.storage.repositories import call_repo, tool_invocation_repo

router = APIRouter(prefix="/tools", tags=["tools"])


@router.post("", status_code=201)
async def register_tool(
    body: RegisterToolRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Register a tool definition.

    Tools are stored as part of agent config. This endpoint
    validates the tool schema and returns it for confirmation.
    Actual attachment to an agent is done via PUT /v1/agents/:id.
    """
    return ok(
        ToolResponse(
            name=body.name,
            description=body.description,
            parameters_schema=body.parameters_schema,
            execution_mode=body.execution_mode,
            webhook_url=body.webhook_url,
            timeout_seconds=body.timeout_seconds,
            max_retries=body.max_retries,
        )
    )


@router.get("/invocations/{call_id}")
async def list_tool_invocations(
    call_id: UUID,
    auth: Auth,
    session: DbSession,
) -> dict:
    """List all tool invocations for a call."""
    call = await call_repo.get_call_by_id(session, call_id, project_id=auth.project_id)
    if call is None:
        raise NotFoundError("Call", str(call_id))

    invocations = await tool_invocation_repo.list_invocations_for_call(session, call_id)
    return ok([ToolInvocationResponse.model_validate(i) for i in invocations])

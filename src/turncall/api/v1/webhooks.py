"""Webhook subscription management endpoints."""

import secrets
from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select, update

from turncall.api.deps import DbSession
from turncall.api.errors import NotFoundError
from turncall.api.responses import ok
from turncall.api.v1.schemas.webhooks import CreateWebhookRequest, WebhookResponse
from turncall.auth import Auth, WriteAuth
from turncall.storage.models import WebhookSubscriptionRow

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("", status_code=201)
async def create_webhook(
    body: CreateWebhookRequest,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Create a webhook subscription.

    A signing secret is generated and returned ONCE. Store it securely.
    """
    secret = secrets.token_urlsafe(32)

    row = WebhookSubscriptionRow(
        project_id=auth.project_id,
        url=body.url,
        secret=secret,
        events={"events": body.events},
        active=True,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)  # populate server-side created_at

    # Same shape as GET (WebhookResponse) + the one-time secret, so the create
    # and list payloads no longer diverge (create used to omit project_id).
    return ok({**WebhookResponse.from_row(row).model_dump(mode="json"), "secret": secret})


@router.get("")
async def list_webhooks(
    auth: Auth,
    session: DbSession,
) -> dict:
    """List webhook subscriptions for the project."""
    result = await session.execute(
        select(WebhookSubscriptionRow)
        .where(WebhookSubscriptionRow.project_id == auth.project_id)
        .order_by(WebhookSubscriptionRow.created_at.desc())
    )
    rows = result.scalars().all()
    return ok([WebhookResponse.from_row(r) for r in rows])


@router.delete("/{webhook_id}")
async def delete_webhook(
    webhook_id: UUID,
    auth: WriteAuth,
    session: DbSession,
) -> dict:
    """Deactivate a webhook subscription."""
    result = await session.execute(
        update(WebhookSubscriptionRow)
        .where(
            WebhookSubscriptionRow.id == webhook_id,
            WebhookSubscriptionRow.project_id == auth.project_id,
        )
        .values(active=False)
    )
    if result.rowcount == 0:  # type: ignore[union-attr]
        raise NotFoundError("Webhook", str(webhook_id))
    return ok({"deactivated": True})

"""create_webhook and list_webhooks must return the same shape (review: create
omitted project_id). The create payload = WebhookResponse fields + one-time secret."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from turncall.api.v1.schemas.webhooks import WebhookResponse


def _row():
    return SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        url="https://hook",
        events={"events": ["call.ended"]},
        active=True,
        created_at=datetime.now(UTC),
    )


@pytest.mark.unit
def test_create_payload_covers_response_fields_plus_secret():
    row = _row()
    payload = {**WebhookResponse.from_row(row).model_dump(mode="json"), "secret": "s"}
    # every field the GET (WebhookResponse) exposes is present...
    for field in WebhookResponse.model_fields:
        assert field in payload, f"create payload missing {field}"
    assert payload["project_id"] == str(row.project_id)  # the field that used to be dropped
    assert payload["secret"] == "s"  # ...plus the one-time secret

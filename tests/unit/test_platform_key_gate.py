"""Platform-credential gate on the bootstrap endpoints (ticket #102).

Project creation and first-API-key creation used to be unauthenticated. They now
require the single platform credential (X-Platform-Key) that only the builder
holds. TurnCall stays identity-free — this is a privileged-caller check, not a
user. Tested through the existing TestClient(app) seam with the DB mocked out."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from turncall.api.deps import get_session
from turncall.auth import dependencies as deps

PLATFORM_KEY = "platform-secret-xyz"


def _configure(app, monkeypatch, *, platform_key: str) -> None:
    """Point the gate at `platform_key` and stub the DB session."""
    monkeypatch.setattr(
        deps,
        "get_settings",
        lambda: SimpleNamespace(auth=SimpleNamespace(platform_api_key=platform_key)),
    )
    app.app.dependency_overrides[get_session] = lambda: AsyncMock()


# --- project creation -----------------------------------------------------


@pytest.mark.unit
def test_create_project_rejected_without_platform_key(app, monkeypatch) -> None:
    _configure(app, monkeypatch, platform_key=PLATFORM_KEY)
    r = app.post("/v1/projects", json={"name": "Acme"})
    assert r.status_code == 401


@pytest.mark.unit
def test_create_project_rejected_with_wrong_platform_key(app, monkeypatch) -> None:
    _configure(app, monkeypatch, platform_key=PLATFORM_KEY)
    r = app.post(
        "/v1/projects", json={"name": "Acme"}, headers={"X-Platform-Key": "nope"}
    )
    assert r.status_code == 401


@pytest.mark.unit
def test_create_project_succeeds_with_platform_key(app, monkeypatch) -> None:
    _configure(app, monkeypatch, platform_key=PLATFORM_KEY)
    now = datetime.now(UTC)
    row = SimpleNamespace(id=uuid.uuid4(), name="Acme", created_at=now, updated_at=now)
    monkeypatch.setattr(
        "turncall.storage.repositories.project_repo.create_project",
        AsyncMock(return_value=row),
    )
    r = app.post(
        "/v1/projects", json={"name": "Acme"}, headers={"X-Platform-Key": PLATFORM_KEY}
    )
    assert r.status_code == 201
    assert r.json()["data"]["name"] == "Acme"


@pytest.mark.unit
def test_gate_fails_closed_when_platform_key_unset(app, monkeypatch) -> None:
    # No configured credential => reject even a caller that sends a (guessed) key.
    _configure(app, monkeypatch, platform_key="")
    r = app.post(
        "/v1/projects", json={"name": "Acme"}, headers={"X-Platform-Key": "anything"}
    )
    assert r.status_code == 401


# --- first API-key creation ----------------------------------------------


@pytest.mark.unit
def test_create_api_key_rejected_without_platform_key(app, monkeypatch) -> None:
    _configure(app, monkeypatch, platform_key=PLATFORM_KEY)
    r = app.post(f"/v1/api-keys?project_id={uuid.uuid4()}", json={"name": "k"})
    assert r.status_code == 401


@pytest.mark.unit
def test_create_api_key_rejected_with_wrong_platform_key(app, monkeypatch) -> None:
    _configure(app, monkeypatch, platform_key=PLATFORM_KEY)
    r = app.post(
        f"/v1/api-keys?project_id={uuid.uuid4()}",
        json={"name": "k"},
        headers={"X-Platform-Key": "nope"},
    )
    assert r.status_code == 401


@pytest.mark.unit
def test_create_api_key_succeeds_with_platform_key(app, monkeypatch) -> None:
    _configure(app, monkeypatch, platform_key=PLATFORM_KEY)
    pid = uuid.uuid4()
    now = datetime.now(UTC)
    monkeypatch.setattr(
        "turncall.storage.repositories.api_key_repo.list_api_keys_for_project",
        AsyncMock(return_value=[]),  # no keys yet -> bootstrap path
    )
    monkeypatch.setattr(
        "turncall.storage.repositories.project_repo.get_project_by_id",
        AsyncMock(return_value=object()),  # project exists
    )
    key_row = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=pid,
        key_prefix="tc_abc",
        name="k",
        role="developer",
        environment=None,
        created_at=now,
    )
    monkeypatch.setattr(
        "turncall.storage.repositories.api_key_repo.create_api_key",
        AsyncMock(return_value=key_row),
    )
    r = app.post(
        f"/v1/api-keys?project_id={pid}",
        json={"name": "k"},
        headers={"X-Platform-Key": PLATFORM_KEY},
    )
    assert r.status_code == 201
    assert r.json()["data"]["raw_key"].startswith("tc_")

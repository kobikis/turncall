"""What an audit log entry records as changed, and that no secret is in it
(#317, ADR-0024). End to end: `tests/integration/test_harness_audit_log.py`."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from turncall.services import audit_log
from turncall.services.audit_log import State, diff

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 17, tzinfo=UTC)


def _agent(**config) -> SimpleNamespace:
    return SimpleNamespace(
        name="support",
        environment="development",
        version=1,
        state="draft",
        published_at=None,
        config_blob=config,
    )


def test_only_what_changed_is_recorded_field_by_field() -> None:
    before = State.of({"a": 1, "nested": {"x": 1, "y": 2}, "list": [1, 2]})
    after = State.of({"a": 1, "nested": {"x": 1, "y": 3}, "list": [1, 5]})
    assert diff(before, after) == {
        "nested.y": {"before": 2, "after": 3},
        "list.1": {"before": 2, "after": 5},
    }


def test_a_create_and_a_delete_record_every_field_set() -> None:
    state = State.of({"name": "a", "unset": None, "config": {"k": "v"}})
    assert diff(None, state) == {
        "name": {"before": None, "after": "a"},
        "config": {"before": None, "after": {"k": "v"}},
    }
    assert diff(state, None) == {
        "name": {"before": "a", "after": None},
        "config": {"before": {"k": "v"}, "after": None},
    }
    assert diff(state, state) == {}


def test_a_list_that_changed_length_is_one_field() -> None:
    assert diff(State.of({"l": [1]}), State.of({"l": [1, 2]})) == {
        "l": {"before": [1], "after": [1, 2]}
    }


def test_a_state_is_a_copy_of_the_row_as_it_was() -> None:
    config = {"system_prompt": "old"}
    row = _agent(**config)
    before = audit_log.agent_state(row)
    config["system_prompt"] = "new"  # the ORM updates the same object
    assert before.public["config"]["system_prompt"] == "old"
    assert before.raw["config"]["system_prompt"] == "old"


def test_an_agents_secrets_are_masked_and_still_seen_to_change() -> None:
    old = _agent(
        llm={"provider": "openai", "api_key": "sk-old"},
        server_url={"url": "https://x", "secret": "s1"},
        mcp_servers=[{"name": "m", "headers": {"Authorization": "Bearer t1"}}],
        tools=[{"name": "t", "webhook_secret": "w1"}],
        aws={"access_key_id": "AKIA", "secret_access_key": "sec1"},
    )
    new = _agent(
        llm={"provider": "openai", "api_key": "sk-new"},
        server_url={"url": "https://x", "secret": "s2"},
        mcp_servers=[{"name": "m", "headers": {"Authorization": "Bearer t2"}}],
        tools=[{"name": "t", "webhook_secret": "w2"}],
        aws={"access_key_id": "AKIA", "secret_access_key": "sec2"},
    )
    changes = diff(audit_log.agent_state(old), audit_log.agent_state(new))
    masked = {"before": "***", "after": "***"}
    assert changes == {
        "config.llm.api_key": masked,
        "config.server_url.secret": masked,
        "config.mcp_servers.0.headers.Authorization": masked,
        "config.tools.0.webhook_secret": masked,
        "config.aws.secret_access_key": masked,
    }
    created = str(diff(None, audit_log.agent_state(new)))
    for secret in ("sk-new", "s2", "Bearer t2", "w2", "sec2"):
        assert secret not in created


def _number(**over) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "id": uuid.uuid4(),
            "project_id": uuid.uuid4(),
            "provider": "twilio",
            "external_number_sid": "PN1",
            "e164_number": "+15550001111",
            "routing_target_type": "webhook",
            "routing_target_id": None,
            "server_url": "https://x/init",
            "server_url_secret": "call-init-secret",
            "sms_enabled": False,
            "routing_weights": None,
            "metadata_json": {},
            "created_at": NOW,
            "twilio_account_sid": "AC1",
            "twilio_auth_token_ciphertext": "gAAAA-old",
            **over,
        }
    )


def test_a_numbers_secret_and_token_are_masked() -> None:
    before = audit_log.phone_number_state(_number())
    after = audit_log.phone_number_state(
        _number(twilio_auth_token_ciphertext="gAAAA-new", sms_enabled=True)
    )
    assert diff(before, after) == {
        "sms_enabled": {"before": False, "after": True},
        "twilio_auth_token": {"before": "***", "after": "***"},
    }
    created = str(diff(None, before))
    assert "call-init-secret" not in created and "gAAAA" not in created
    assert diff(None, before)["server_url_secret"] == {"before": None, "after": "***"}


def test_a_credential_is_recorded_as_present_never_as_its_value() -> None:
    old = SimpleNamespace(ciphertext="gAAAA-1")
    new = SimpleNamespace(ciphertext="gAAAA-2")
    assert audit_log.credential_state(None) is None
    assert diff(None, audit_log.credential_state(new)) == {
        "value": {"before": None, "after": "***"}
    }
    assert diff(audit_log.credential_state(old), audit_log.credential_state(new)) == {
        "value": {"before": "***", "after": "***"}
    }


def test_an_api_key_is_recorded_without_its_material() -> None:
    row = SimpleNamespace(
        id=uuid.uuid4(),
        key_prefix="tc_live_abcd",
        key_hash="deadbeef",
        name="ci",
        role="developer",
        environment=None,
        revoked_at=None,
    )
    recorded = diff(None, audit_log.api_key_state(row))
    assert recorded == {
        "name": {"before": None, "after": "ci"},
        "role": {"before": None, "after": "developer"},
    }


async def test_a_trail_names_every_entry_it_wrote(monkeypatch) -> None:
    written: list[dict] = []

    async def append(_session, **values) -> None:
        written.append(values)

    monkeypatch.setattr(audit_log.audit_repo, "append", append)
    seen: list[list[uuid.UUID]] = []
    project = uuid.uuid4()
    trail = audit_log.AuditTrail(
        session=None,  # type: ignore[arg-type]
        actor=audit_log.AuditActor.API_KEY,
        api_key_id=uuid.uuid4(),
        project_id=project,
        on_record=seen.append,
    )
    with pytest.raises(audit_log.AuditEntryMissing):
        trail.require_entry("PUT /v1/x")
    first = await trail.record(
        audit_log.AuditResource.AGENT, "a", audit_log.AuditAction.UPDATED
    )
    second = await trail.record(
        audit_log.AuditResource.AGENT, "b", audit_log.AuditAction.ARCHIVED
    )
    trail.require_entry("PUT /v1/x")
    assert seen == [[first], [first, second]]
    assert [w["id"] for w in written] == [first, second]
    assert written[0]["project_id"] == project and written[0]["changes"] == {}

    platform = audit_log.AuditTrail(
        session=None,  # type: ignore[arg-type]
        actor=audit_log.AuditActor.PLATFORM,
        api_key_id=None,
        project_id=None,
    )
    with pytest.raises(ValueError, match="project"):
        await platform.record(
            audit_log.AuditResource.PROJECT, "p", audit_log.AuditAction.CREATED
        )

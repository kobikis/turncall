"""What an audit log entry records as changed, and that no secret is in it
(#317, ADR-0024). End to end: `tests/integration/test_harness_audit_log.py`."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.config.settings import Settings
from turncall.domain.enums import AuditAction, AuditResource
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
        "/nested/y": {"before": 2, "after": 3},
        "/list/1": {"before": 2, "after": 5},
    }


def test_a_path_is_a_json_pointer_so_no_two_collide() -> None:
    """A key may hold a `.` or a `/` (metadata, an MCP header): each path is
    an RFC 6901 pointer, escaped, so a dotted key is not a nested one."""
    before = State.of(
        {"metadata": {"team.name": "a", "team": {"name": 1}, "a/b": 0, "x~y": 0}}
    )
    after = State.of(
        {"metadata": {"team.name": "b", "team": {"name": 2}, "a/b": 1, "x~y": 1}}
    )
    assert diff(before, after) == {
        "/metadata/a~1b": {"before": 0, "after": 1},
        "/metadata/team/name": {"before": 1, "after": 2},
        "/metadata/team.name": {"before": "a", "after": "b"},
        "/metadata/x~0y": {"before": 0, "after": 1},
    }


def test_a_create_and_a_delete_record_every_field_set() -> None:
    state = State.of({"name": "a", "unset": None, "config": {"k": "v"}})
    assert diff(None, state) == {
        "/name": {"before": None, "after": "a"},
        "/config": {"before": None, "after": {"k": "v"}},
    }
    assert diff(state, None) == {
        "/name": {"before": "a", "after": None},
        "/config": {"before": {"k": "v"}, "after": None},
    }
    assert diff(state, state) == {}


def test_a_list_that_changed_length_is_one_field() -> None:
    assert diff(State.of({"l": [1]}), State.of({"l": [1, 2]})) == {
        "/l": {"before": [1], "after": [1, 2]}
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
        "/config/llm/api_key": masked,
        "/config/server_url/secret": masked,
        "/config/mcp_servers/0/headers/Authorization": masked,
        "/config/tools/0/webhook_secret": masked,
        "/config/aws/secret_access_key": masked,
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
        "/sms_enabled": {"before": False, "after": True},
        "/twilio_auth_token": {"before": "***", "after": "***"},
    }
    created = str(diff(None, before))
    assert "call-init-secret" not in created and "gAAAA" not in created
    assert diff(None, before)["/server_url_secret"] == {"before": None, "after": "***"}


def test_a_credential_is_recorded_as_present_never_as_its_value() -> None:
    old = SimpleNamespace(ciphertext="gAAAA-1")
    new = SimpleNamespace(ciphertext="gAAAA-2")
    assert audit_log.credential_state(None) is None
    assert diff(None, audit_log.credential_state(new)) == {
        "/value": {"before": None, "after": "***"}
    }
    assert diff(audit_log.credential_state(old), audit_log.credential_state(new)) == {
        "/value": {"before": "***", "after": "***"}
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
        "/name": {"before": None, "after": "ci"},
        "/role": {"before": None, "after": "developer"},
    }


def _trail(**over) -> tuple[audit_log.AuditTrail, AsyncSession]:
    session = AsyncSession()  # bound to nothing: commits run their hooks only
    values = {
        "actor": audit_log.AuditActor.API_KEY,
        "api_key_id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        **over,
    }
    return audit_log.AuditTrail.bind(session, **values), session


async def test_a_requests_entries_are_written_in_one_insert_as_it_commits(
    monkeypatch,
) -> None:
    inserts: list[list[dict]] = []
    monkeypatch.setattr(
        audit_log.audit_repo, "insert_entries", lambda _s, rows: inserts.append(rows)
    )
    named: list[uuid.UUID] = []
    trail, session = _trail(on_first=named.append)
    first = trail.record(AuditResource.AGENT, "a", AuditAction.PUBLISHED)
    second = trail.record(AuditResource.AGENT, "b", AuditAction.ARCHIVED)
    third = trail.record(AuditResource.PHONE_NUMBER, "n", AuditAction.UPDATED)
    assert inserts == []  # nothing written until the commit

    await session.commit()

    (rows,) = inserts
    assert [r["id"] for r in rows] == [first, second, third]
    # The first entry names the request, once, and every entry carries it.
    assert named == [first]
    assert {r["request_id"] for r in rows} == {first}
    assert rows[0]["project_id"] == trail.project_id and rows[0]["changes"] == {}
    await session.commit()  # nothing pending: nothing written again
    assert len(inserts) == 1


async def test_a_commit_of_a_request_that_recorded_nothing_is_refused(
    monkeypatch,
) -> None:
    """Whoever commits: the route itself, or DbSession at the end."""
    monkeypatch.setattr(audit_log.audit_repo, "insert_entries", lambda *_: None)
    _, session = _trail(where="PUT /v1/credentials/openai")
    with pytest.raises(audit_log.AuditEntryMissing, match="PUT /v1/credentials"):
        await session.commit()
    # A session with no trail (not an audited request) commits as ever.
    await AsyncSession().commit()


def test_a_platform_entry_names_its_project() -> None:
    trail, _ = _trail(
        actor=audit_log.AuditActor.PLATFORM, api_key_id=None, project_id=None
    )
    with pytest.raises(ValueError, match="project"):
        trail.record(AuditResource.PROJECT, "p", AuditAction.CREATED)


def test_a_change_takes_its_before_on_entry() -> None:
    """The repos update the row read in place: a "before" taken after the
    write would equal the "after". `change` takes it before the block runs."""
    trail, _ = _trail()
    row = _agent(system_prompt="old")
    with trail.change(
        AuditResource.AGENT, "a", AuditAction.UPDATED, audit_log.agent_state, row
    ) as change:
        row.config_blob["system_prompt"] = "new"  # what the ORM does
        change.to(row)
    (entry,) = trail.pending
    assert entry["changes"] == {
        "/config/system_prompt": {"before": "old", "after": "new"}
    }


def test_a_change_must_say_what_it_became_and_a_failed_one_is_not_recorded() -> None:
    trail, _ = _trail()
    with (
        pytest.raises(RuntimeError, match=r"change\.to"),
        trail.change(
            AuditResource.AGENT,
            "a",
            AuditAction.UPDATED,
            audit_log.agent_state,
            _agent(),
        ),
    ):
        pass
    with (
        pytest.raises(LookupError),
        trail.change(
            AuditResource.AGENT,
            "a",
            AuditAction.UPDATED,
            audit_log.agent_state,
            _agent(),
        ) as change,
    ):
        raise LookupError  # the write failed
    with trail.change(
        AuditResource.CREDENTIAL,
        "openai",
        AuditAction.DELETED,
        audit_log.credential_state,
        None,
    ) as change:
        change.gone()  # nothing was there: an entry with nothing changed
    assert [e["changes"] for e in trail.pending] == [{}]


@pytest.mark.parametrize("value", ["", "  "])
def test_a_blank_audit_log_retention_is_unset(monkeypatch, value) -> None:
    monkeypatch.setenv("AUDIT_LOG_RETENTION_DAYS", value)
    assert Settings().audit_log_retention_days is None
    monkeypatch.setenv("AUDIT_LOG_RETENTION_DAYS", "90")
    assert Settings().audit_log_retention_days == 90

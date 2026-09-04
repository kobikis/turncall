"""Guards against the API schema silently dropping the avatar config.

The bug: AgentConfigSchema (API ingest) lacked an `avatar` field, so an
avatar-enabled agent created via the API stored a config with no avatar and the
pipeline never built the HeyGen service. This asserts avatar survives the
request-schema -> config_blob -> domain-model round-trip.
"""

from turncall.api.v1.schemas.agents import AgentConfigSchema
from turncall.domain.models import AgentConfig


def test_avatar_survives_schema_roundtrip() -> None:
    schema = AgentConfigSchema.model_validate(
        {"avatar": {"enabled": True, "avatar_id": "av_123"}}
    )
    # model_dump() is exactly what create_agent persists as config_blob.
    domain = AgentConfig.model_validate(schema.model_dump())
    assert domain.avatar.enabled is True
    assert domain.avatar.avatar_id == "av_123"
    assert domain.avatar.provider == "heygen"


def test_avatar_defaults_off() -> None:
    domain = AgentConfig.model_validate(AgentConfigSchema().model_dump())
    assert domain.avatar.enabled is False


def test_tavus_provider_survives_roundtrip() -> None:
    schema = AgentConfigSchema.model_validate(
        {"avatar": {"enabled": True, "provider": "tavus", "replica_id": "r_123"}}
    )
    domain = AgentConfig.model_validate(schema.model_dump())
    assert domain.avatar.provider == "tavus"
    assert domain.avatar.replica_id == "r_123"
    assert domain.avatar.persona_id == "pipecat-stream"


if __name__ == "__main__":
    test_avatar_survives_schema_roundtrip()
    test_avatar_defaults_off()
    test_tavus_provider_survives_roundtrip()
    print("OK")

"""The 422 handler must survive model_validator errors (they carry a raw
ValueError in pydantic's `ctx`, which used to make the handler itself 500)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from turncall.api.errors import register_error_handlers
from turncall.api.v1.schemas.agents import S2SConfigSchema


def _client() -> TestClient:
    app = FastAPI()
    register_error_handlers(app)

    @app.post("/echo")
    def echo(body: S2SConfigSchema) -> dict:  # validates the body on parse
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


def test_model_validator_error_returns_422_not_500():
    # Invalid OpenAI Realtime voice -> S2SConfigSchema.validate_voice raises
    # ValueError -> RequestValidationError with a non-serializable ctx.
    resp = _client().post("/echo", json={"provider": "openai", "voice": "Kore"})

    assert resp.status_code == 422  # not 500
    body = resp.json()  # response is valid JSON (the bug made it unserializable)
    assert body["code"] == "validation_error"
    # the human-readable reason survives into the error details
    assert "Invalid OpenAI Realtime voice" in str(body["details"])


def test_valid_body_passes():
    resp = _client().post("/echo", json={"provider": "openai", "voice": "alloy"})
    assert resp.status_code == 200

"""The rest of the review's low-severity list.

Small each, but two of them mislead: a schema that loses its `$defs` is
rejected by the provider rather than failing here, and an invocation marked
`failed` is what someone reads when they later ask why a call went wrong.
"""

import pytest

from tests.conftest import mcp_tool
from turncall.services.mcp_client import _mcp_tool_to_definition
from turncall.services.tool_webhook import classify_tool_result


@pytest.mark.unit
class TestDiscoveredSchemasSurviveIntact:
    def test_defs_and_refs_are_not_dropped(self) -> None:
        """Rebuilding the schema from type/properties/required left any
        `$ref` in properties pointing at nothing. Providers reject an
        unresolvable `$ref`, so the tool failed at the model, not here."""
        schema = {
            "type": "object",
            "properties": {"to": {"$ref": "#/$defs/Address"}},
            "required": ["to"],
            "$defs": {
                "Address": {
                    "type": "object",
                    "properties": {"street": {"type": "string"}},
                }
            },
            "additionalProperties": False,
            "description": "Where to deliver it",
        }

        tool = _mcp_tool_to_definition(mcp_tool("deliver", schema), "crm")

        assert tool.parameters_schema == schema

    def test_a_server_that_omits_the_basics_still_gets_a_valid_schema(self) -> None:
        tool = _mcp_tool_to_definition(mcp_tool("ping", {}), "crm")

        assert tool.parameters_schema["type"] == "object"
        assert tool.parameters_schema["properties"] == {}

    def test_a_declared_type_is_not_overwritten(self) -> None:
        schema = {"type": "object", "properties": {"q": {"type": "string"}}}
        tool = _mcp_tool_to_definition(mcp_tool("search", schema), "docs")

        assert tool.parameters_schema["properties"] == {"q": {"type": "string"}}


@pytest.mark.unit
class TestResultClassification:
    @pytest.mark.parametrize(
        "payload",
        ['{"error": null}', '{"error": ""}', '{"result": 42, "error": null}'],
    )
    def test_an_empty_error_field_is_not_a_failure(self, payload: str) -> None:
        """Plenty of endpoints always include the key. Reading presence rather
        than value marked every one of their successful calls as failed."""
        status, _ = classify_tool_result(payload)

        assert status == "succeeded"

    def test_a_real_error_is_still_a_failure(self) -> None:
        status, out = classify_tool_result('{"error": "no such customer"}')

        assert status == "failed"
        assert out == {"error": "no such customer"}

    def test_a_plain_success_is_unaffected(self) -> None:
        status, out = classify_tool_result('{"balance": 42}')

        assert status == "succeeded"
        assert out == {"balance": 42}

    def test_non_json_is_carried_through_as_text(self) -> None:
        status, out = classify_tool_result("OK")

        assert status == "succeeded"
        assert out == {"result": "OK"}


@pytest.mark.unit
def test_the_manager_keeps_no_unread_state() -> None:
    """`_sessions` and `_connected` were written on every connect and read
    nowhere. Open sessions are held by the exit stack and by _tool_refs."""
    import uuid

    from turncall.services.mcp_client import MCPSessionManager

    manager = MCPSessionManager(call_id=uuid.uuid4(), project_id=uuid.uuid4())

    assert not hasattr(manager, "_connected")
    assert not hasattr(manager, "_sessions")

"""A skill's knowledge bases on a voice call (#429), over the running app.

A loaded skill's `prompt`-mode knowledge bases put their documents in that
skill's own prompt section, and its `tool`-mode ones are searched through
`query_knowledge` — added by the load, with the hint, for an agent that has
none, and the agent's own tool for one that has (searching both). Before the
load neither is in the prompt nor searchable; an unload (the loaded-skills
limit) takes both away again. An `auto`-mode attachment is not applied.
Seam A: the real app, stub providers, a fake carrier; documents and chunks
are written straight to the database, the query's embedding stubbed.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.harness import HARNESS_AGENT, Given, Script, ToolCall, run_app
from tests.harness.twilio import place_inbound_call
from turncall.config.settings import Settings
from turncall.services.retrieval import KNOWLEDGE_TOOL_HINT
from turncall.storage.database import create_engine, create_session_factory
from turncall.storage.repositories import knowledge_repo

pytestmark = pytest.mark.integration

CALLER = "+15550001234"
SAID = "Why is my bill higher?"
BILL = "Use when the caller asks why a bill changed."
LATE_FEES = "Late fees are fifteen dollars after the due date."
ROAMING = "Roaming costs three euros per day in the EU."
AUTO_ONLY = "Paperless billing saves two dollars a month."
STORE = "Our store opens at nine in the morning."
# One vector for every chunk and every query: the vector leg then matches
# whatever chunks the searched knowledge bases hold, and nothing else.
VECTOR = [1.0] + [0.0] * 1535


async def _embeddings(texts: list[str], **_kwargs: Any) -> list[list[float]]:
    return [VECTOR for _ in texts]


def _remembering(given: list[Given], reply: Any):
    def answer(context: Given) -> Any:
        given.append(context)
        return reply

    return answer


def _prompt(given: Given) -> str:
    return next((m["content"] for m in given if m["role"] == "system"), "")


def _tool_results(given: Given) -> list[str]:
    return [str(m.get("content")) for m in given if m["role"] == "tool"]


async def _kb(app: Any, name: str, text: str) -> str:
    """A knowledge base of the project holding one ready document of `text`,
    as one embedded chunk."""
    created = await app.api.post("/v1/knowledge-bases", json={"name": name})
    assert created.status_code == 201, created.text
    kb_id = created.json()["data"]["id"]
    engine = create_engine(Settings().database)
    try:
        async with create_session_factory(engine)() as session:
            doc = await knowledge_repo.create_document(
                session,
                knowledge_base_id=uuid.UUID(kb_id),
                filename=f"{name}.txt",
                content_type="text/plain",
                storage_key=f"test/{uuid.uuid4()}",
                raw_text=text,
                char_count=len(text),
                chunk_count=1,
                status="ready",
            )
            await knowledge_repo.create_chunks_batch(
                session,
                [
                    {
                        "document_id": doc.id,
                        "knowledge_base_id": uuid.UUID(kb_id),
                        "chunk_index": 0,
                        "content": text,
                        "token_count": len(text.split()),
                        "embedding": VECTOR,
                    }
                ],
            )
            await session.commit()
    finally:
        await engine.dispose()
    return kb_id


async def _publish_skill(app: Any, body: dict[str, Any]) -> dict[str, Any]:
    created = await app.api.post("/v1/skills", json=body)
    assert created.status_code == 201, created.text
    skill = created.json()["data"]
    published = await app.api.post(f"/v1/skills/{skill['id']}/publish")
    assert published.status_code == 200, published.text
    return dict(published.json()["data"])


async def _run_call(app: Any, agent: dict[str, Any]) -> None:
    number = await app.bind_number(await app.create_agent(agent))
    call = await place_inbound_call(
        app.carrier, app.api, from_number=CALLER, to_number=number
    )
    await call.wait_for_agent_speech(1)  # the greeting
    await call.say(400)
    await call.wait_for_agent_speech(2)
    await call.hang_up()
    await app.sink.wait_for("call.ended")


async def test_a_loaded_skill_brings_its_documents_and_its_search(monkeypatch):
    before: list[Given] = []
    after: list[Given] = []
    searched: list[Given] = []
    script = Script(
        caller=(SAID,),
        agent=(
            _remembering(before, ToolCall("load_skill", {"name": "explain_bill"})),
            _remembering(after, ToolCall("query_knowledge", {"query": "roaming"})),
            _remembering(searched, "Roaming is three euros a day."),
        ),
    )
    monkeypatch.setattr(
        "turncall.services.document_ingestion.generate_embeddings", _embeddings
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        faq = await _kb(app, "billing_faq", LATE_FEES)
        tariffs = await _kb(app, "tariffs", ROAMING)
        auto = await _kb(app, "paperless", AUTO_ONLY)
        skill = await _publish_skill(
            app,
            {
                "name": "explain_bill",
                "description": BILL,
                "instructions": "Compare the last two invoices.",
                "knowledge_bases": [
                    {"knowledge_base_id": faq, "mode": "prompt"},
                    {
                        "knowledge_base_id": tariffs,
                        "mode": "tool",
                        "tool_description": "Search the tariff sheet",
                    },
                    {"knowledge_base_id": auto},  # auto, the default
                ],
            },
        )
        await _run_call(
            app,
            {
                **HARNESS_AGENT,
                "first_message": "Hello.",
                "skills": [{"skill": "explain_bill"}],
            },
        )

    # Warned on the skill, and never applied below.
    [warning] = skill["warnings"]
    assert warning["code"] == "knowledge_mode_unsupported"
    assert warning["knowledge_base_ids"] == [auto]

    # Before the load: neither the documents nor the search.
    [first] = before
    assert LATE_FEES not in _prompt(first)
    assert KNOWLEDGE_TOOL_HINT not in _prompt(first)
    assert first.tools == ["load_skill"]

    # After it: the documents in the skill's own section, and query_knowledge
    # (with its hint) added for this agent, which had none.
    [second] = after
    prompt = _prompt(second)
    section = prompt[prompt.index("## Skill: explain_bill (v1)") :]
    assert section.startswith(
        "## Skill: explain_bill (v1)\nCompare the last two invoices.\n\n"
        "[Knowledge Base Context]"
    ), section
    assert LATE_FEES in section
    assert KNOWLEDGE_TOOL_HINT in prompt
    assert second.tools == ["load_skill", "query_knowledge"]

    # The search returns the skill's tool-mode passages, never the auto KB's.
    [third] = searched
    [result] = [r for r in _tool_results(third) if "Knowledge Base Context" in r]
    assert ROAMING in result
    assert LATE_FEES not in result
    assert all(AUTO_ONLY not in str(m.get("content")) for m in third)
    assert AUTO_ONLY not in _prompt(third)


async def test_the_agents_own_search_covers_a_loaded_skill_until_it_is_unloaded(
    monkeypatch,
):
    """An agent with its own tool-mode KB offers query_knowledge from the
    start: before the load it searches the agent's KB only, after it the
    skill's too under the same tool, and once the skill is unloaded to make
    room for another (max_loaded_skills 1) only the agent's again, the
    skill's documents gone from the prompt."""
    given: list[Given] = []
    script = Script(
        caller=(SAID,),
        agent=(
            ToolCall("query_knowledge", {"query": "roaming"}),
            _remembering(given, ToolCall("load_skill", {"name": "explain_bill"})),
            _remembering(given, ToolCall("query_knowledge", {"query": "roaming"})),
            _remembering(given, ToolCall("load_skill", {"name": "verify_identity"})),
            _remembering(given, ToolCall("query_knowledge", {"query": "roaming"})),
            _remembering(given, "Done."),
        ),
    )
    monkeypatch.setattr(
        "turncall.services.document_ingestion.generate_embeddings", _embeddings
    )
    async with run_app(
        script, set_env=monkeypatch.setenv, set_attr=monkeypatch.setattr
    ) as app:
        store = await _kb(app, "store", STORE)
        faq = await _kb(app, "billing_faq", LATE_FEES)
        tariffs = await _kb(app, "tariffs", ROAMING)
        await _publish_skill(
            app,
            {
                "name": "explain_bill",
                "description": BILL,
                "instructions": "Compare the last two invoices.",
                "knowledge_bases": [
                    {"knowledge_base_id": faq, "mode": "prompt"},
                    {
                        "knowledge_base_id": tariffs,
                        "mode": "tool",
                        "tool_description": "Search the tariff sheet",
                    },
                ],
            },
        )
        await _publish_skill(
            app,
            {
                "name": "verify_identity",
                "description": "Use before account details.",
                "instructions": "Ask for the date of birth.",
            },
        )
        agent_id = await app.create_agent(
            {
                **HARNESS_AGENT,
                "first_message": "Hello.",
                "max_loaded_skills": 1,
                "skills": [{"skill": "explain_bill"}, {"skill": "verify_identity"}],
            }
        )
        linked = await app.api.post(
            f"/v1/agents/{agent_id}/knowledge-bases",
            json={
                "knowledge_base_id": store,
                "mode": "tool",
                "tool_description": "Search the store's FAQ",
            },
        )
        assert linked.status_code == 201, linked.text
        number = await app.bind_number(agent_id)
        call = await place_inbound_call(
            app.carrier, app.api, from_number=CALLER, to_number=number
        )
        await call.wait_for_agent_speech(1)
        await call.say(400)
        await call.wait_for_agent_speech(2)
        await call.hang_up()
        await app.sink.wait_for("call.ended")

    loading, after_load, unloading, after_unload, last = given

    def latest_search(context: Given) -> str:
        return [r for r in _tool_results(context) if "count" in r][-1]

    # Before the load: the agent's KB only.
    assert STORE in latest_search(loading) and ROAMING not in latest_search(loading)
    assert LATE_FEES not in _prompt(loading)
    assert loading.tools.count("query_knowledge") == 1

    # Loaded: one query_knowledge, the agent's, now over both, and the hint
    # once.
    assert after_load.tools.count("query_knowledge") == 1
    assert LATE_FEES in _prompt(after_load)
    assert _prompt(after_load).count(KNOWLEDGE_TOOL_HINT) == 1
    found = latest_search(unloading)
    assert STORE in found and ROAMING in found, found

    # Unloaded for verify_identity: its documents and its search gone.
    assert "## Skill: verify_identity" in _prompt(after_unload)
    assert LATE_FEES not in _prompt(after_unload)
    assert after_unload.tools.count("query_knowledge") == 1
    found = latest_search(last)
    assert STORE in found and ROAMING not in found, found

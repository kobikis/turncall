"""Tool calling for text sessions — SMS, the Chat API and WhatsApp text.

The voice pipeline registers tools through orchestrator/tool_bridge, which
needs a CallContext: every built-in it dispatches resolves through
call_control against a live call_id. A text session has a session_id and no
call, so it gets the customer's webhook tools, anything discovered from
their MCP servers and `query_knowledge` for a `tool`-mode knowledge base —
none of the built-ins but `load_skill` (#430), which needs no call: an agent
with skills gets it, and a load in one round of the tool loop changes what
the next is given (`ChatTools.current`).

Precedence is the voice path's: `load_skill`, the agent's own tools, loaded
skills' tools in load order, MCP, then `query_knowledge` — the first of a name
is what is advertised and what runs.

MCP sessions are opened per inbound message and closed with `aclose()`. That
costs a handshake per reply, but a text request is a single task start to
finish, so connect and close land in the same one — which is what MCP's anyio
cancel scopes require.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from loguru import logger

from turncall.domain.models import (
    BUILTIN_TOOL_NAMES,
    AgentConfig,
    ToolDefinition,
)
from turncall.services.retrieval import KNOWLEDGE_TOOL_SCHEMA, query_knowledge
from turncall.services.skills import LOAD_SKILL
from turncall.services.tool_webhook import invocation_record, post_tool_webhook

if TYPE_CHECKING:
    from turncall.services.text_skills import SessionSkills

QUERY_KNOWLEDGE = KNOWLEDGE_TOOL_SCHEMA["name"]

# Recording tasks kept referenced so asyncio (which holds only a weak ref)
# can't GC one mid-write.
_RECORDING: set[asyncio.Task] = set()


async def _record_invocation(
    tool_name: str,
    args: dict[str, Any],
    result: str,
    latency_ms: int,
    *,
    session_id: UUID,
    project_id: UUID,
) -> None:
    """Persist the invocation and dispatch tool.result, as the voice path does.

    Runs off the reply's critical path: the row is small but the webhook
    dispatch can retry for ~90s against a dead subscriber, and the customer is
    waiting on a message. Failures are logged, never raised — losing the audit
    row must not cost someone their reply.
    """
    from turncall.domain.enums import CallEventType
    from turncall.events.emit import dispatch_project_event, redact
    from turncall.storage.database import get_session_factory
    from turncall.storage.repositories import tool_invocation_repo

    try:
        # Before the session: no detector wait inside a transaction (events/emit).
        payload = await redact(
            project_id,
            CallEventType.TOOL_RESULT,
            {"tool_name": tool_name, "arguments": args, "result": result},
        )
        status, input_json, output_json = await invocation_record(
            project_id, args, result
        )
        async with get_session_factory()() as db:
            await tool_invocation_repo.create_invocation(
                db,
                session_id=session_id,
                tool_name=tool_name,
                input_json=input_json,
                status=status,
                output_json=output_json,
                latency_ms=latency_ms,
            )
            await db.commit()

            await dispatch_project_event(
                db,
                project_id=project_id,
                event_type=CallEventType.TOOL_RESULT,
                payload=payload,
                session_id=session_id,
            )
    except Exception:
        logger.exception("chat_tool_record_failed", tool=tool_name)


@dataclass(frozen=True)
class ChatTools:
    """What a text turn needs to call tools: the schemas to advertise, an
    executor to run them, and the cleanup for any MCP sessions opened.

    `schemas` is what the turn begins with; `current()` is what it offers now,
    after any `load_skill` so far (#430)."""

    schemas: list[dict[str, Any]]
    execute: Callable[[str, dict[str, Any]], Awaitable[str]]
    aclose: Callable[[], Awaitable[None]]
    current: Callable[[], list[dict[str, Any]]]


def _to_function_schema(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters_schema or {"type": "object", "properties": {}},
    }


def _link_settings(link: Any) -> dict[str, Any]:
    """A knowledge-base link (a row, or an inline agent's attachment) in the
    dict shape a skill's tool-mode attachments take (#429)."""
    return {
        "knowledge_base_id": link.knowledge_base_id,
        "top_k": link.top_k,
        "similarity_threshold": link.similarity_threshold,
        "tool_description": getattr(link, "tool_description", None),
    }


async def build_chat_tools(
    config: AgentConfig,
    *,
    session_id: UUID,
    project_id: UUID,
    knowledge: list[Any] | None = None,
    openai_api_key: str = "",
    skills: SessionSkills | None = None,
) -> ChatTools:
    """Assemble the tools available to one text turn.

    `knowledge` is the agent's `tool`-mode knowledge-base links: with any,
    the model gets `query_knowledge` over all of them, as a voice call does.
    `skills` are the session's (`text_skills`): `load_skill`, and the loaded
    skills' tools and tool-mode knowledge bases, read at each use.
    """
    dropped = [t.name for t in config.tools if t.name in BUILTIN_TOOL_NAMES]
    if dropped:
        logger.debug("chat_tools_builtins_skipped", tools=dropped)
    skilled = skills is not None and bool(skills.available)

    def configured() -> dict[str, ToolDefinition]:
        """The turn's own tools by name, first of a name kept: `load_skill`
        (with skills), the agent's, then the loaded skills'."""
        tools = skills.config().tools if skills is not None else config.tools
        own: dict[str, ToolDefinition] = {}
        for t in tools:
            builtin = t.name in BUILTIN_TOOL_NAMES
            if t.name in own or (builtin and not (skilled and t.name == LOAD_SKILL)):
                continue
            own[t.name] = t
        return own

    mcp_manager: Any | None = None
    mcp_tools: list[ToolDefinition] = []
    if config.mcp_servers:
        from turncall.services.mcp_client import MCPSessionManager

        # No call, but the manager tags its requests with these — a session id
        # is the closest thing a text conversation has to a call id.
        mcp_manager = MCPSessionManager(call_id=session_id, project_id=project_id)
        try:
            mcp_tools = await mcp_manager.connect_servers(config.mcp_servers)
        except Exception:
            # One unreachable MCP server must not cost the customer their reply.
            logger.exception("chat_tools_mcp_connect_failed", session=str(session_id))
            mcp_tools = []

    # Static tools are the customer's own, so they win a name clash — same
    # precedence the voice path applies in _build_tools_schema.
    shadowed = {t.name for t in mcp_tools} & configured().keys()
    if shadowed:
        logger.warning("chat_tools_mcp_name_collision", tools=sorted(shadowed))

    own_links = [_link_settings(link) for link in knowledge or []]

    def searched() -> list[dict[str, Any]]:
        """The KBs `query_knowledge` searches now: the agent's tool-mode
        links, then the loaded skills' (#429), each once."""
        return skills.searched(own_links) if skills is not None else own_links

    def offered(own: dict[str, ToolDefinition]) -> list[dict[str, Any]]:
        """The turn's tools, in precedence order."""
        schemas = [_to_function_schema(t) for t in own.values()]
        schemas += [_to_function_schema(t) for t in mcp_tools if t.name not in own]
        links = searched()
        # Last, as on a voice call: a tool of the agent's own by that name
        # stays theirs. Settings come from the first link searched.
        if links and QUERY_KNOWLEDGE not in {s["name"] for s in schemas}:
            schemas.append(
                {
                    **KNOWLEDGE_TOOL_SCHEMA,
                    "description": links[0].get("tool_description")
                    or KNOWLEDGE_TOOL_SCHEMA["description"],
                }
            )
        return schemas

    def current() -> list[dict[str, Any]]:
        return offered(configured())

    schemas = current()
    if (
        searched()
        and QUERY_KNOWLEDGE in {t.name for t in mcp_tools} | configured().keys()
    ):
        logger.warning("chat_tools_knowledge_name_collision", tool=QUERY_KNOWLEDGE)

    async def _run(name: str, args: dict[str, Any]) -> str:
        own = configured()
        if skilled and name == LOAD_SKILL and skills is not None:
            return skills.load(args)
        taken = own.keys() | {t.name for t in mcp_tools}
        links = searched()
        if links and name == QUERY_KNOWLEDGE and name not in taken:
            return await _search(links, args, openai_api_key)
        if (
            mcp_manager is not None
            and name not in own
            and mcp_manager.is_mcp_tool(name)
        ):
            return await mcp_manager.call_tool(name, args)

        tool = own.get(name)
        if tool is None:
            if name in BUILTIN_TOOL_NAMES:
                return json.dumps(
                    {"error": f"{name} is only available on a voice call"}
                )
            return json.dumps({"error": f"Unknown tool: {name}"})

        return await post_tool_webhook(
            tool, args, project_id=project_id, session_id=session_id
        )

    async def execute(name: str, args: dict[str, Any]) -> str:
        started = time.perf_counter()
        result = await _run(name, args)
        latency_ms = int((time.perf_counter() - started) * 1000)

        task = asyncio.create_task(
            _record_invocation(
                name,
                args,
                result,
                latency_ms,
                session_id=session_id,
                project_id=project_id,
            )
        )
        _RECORDING.add(task)
        task.add_done_callback(_RECORDING.discard)
        return result

    async def aclose() -> None:
        if mcp_manager is not None:
            await mcp_manager.close()

    return ChatTools(schemas=schemas, execute=execute, aclose=aclose, current=current)


async def _search(
    links: list[dict[str, Any]], args: dict[str, Any], openai_api_key: str
) -> str:
    """`query_knowledge` over `links` (settings from the first). A failed
    search is an error the model reads, never a raise: losing the lookup must
    not cost the customer their reply."""
    from turncall.storage.database import get_session_factory

    first = links[0]
    try:
        async with get_session_factory()() as db:
            found = await query_knowledge(
                db,
                query=str(args.get("query", "")),
                knowledge_base_ids=[link["knowledge_base_id"] for link in links],
                top_k=first["top_k"],
                similarity_threshold=first["similarity_threshold"],
                openai_api_key=openai_api_key,
            )
    except Exception:
        # Why goes to the log, not the model: it can repeat it to the
        # customer word for word.
        logger.exception("chat_query_knowledge_failed")
        return json.dumps({"error": "knowledge search failed"})
    return json.dumps(found)

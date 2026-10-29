"""A skill's knowledge bases on a conversation (#429).

A skill version names knowledge bases the way an inline agent does
(`KnowledgeBaseAttachment`, checked against the project at save, #423). They
are resolved with the skill, once, when the call's skills are (#424): each KB
checked against the call's project again — a KB deleted since the save is a
warning and a skipped attachment — and `prompt`-mode documents read then, so
loading the skill later costs no I/O.

What a loaded skill's KBs do:

- `prompt`: the documents' text joins the skill's own prompt section
  (`skills.loaded_section`), the way an agent's prompt-mode KB leads its
  prompt.
- `tool`: searched through `query_knowledge`, one tool for the agent's own
  tool-mode KBs and every loaded skill's (`skill_loader`).
- `auto`: not applied. Auto retrieval is a processor in the pipeline, built
  with the call, ahead of every LLM turn; a skill is loaded mid-call, after
  that. Accepted at save, a warning on the skill read (`auto_mode_warning`)
  and at resolution, never a refusal — the attachment does its part once
  switched to `tool` or `prompt`.

No pipecat here: the text channels will build on the same rules (#430).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from loguru import logger
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from turncall.domain.enums import KnowledgeRetrievalMode
from turncall.domain.models import KnowledgeBaseAttachment

AUTO = KnowledgeRetrievalMode.AUTO.value


@dataclass(frozen=True)
class SkillKnowledge:
    """What a skill's knowledge bases give a call once it is loaded: the
    `prompt`-mode documents' text, and the `tool`-mode attachments
    `query_knowledge` searches (the link rows' dict shape)."""

    text: str = ""
    search: tuple[dict[str, Any], ...] = ()


NO_KNOWLEDGE = SkillKnowledge()


def auto_mode_warning(knowledge_bases: Sequence[Any]) -> dict[str, Any] | None:
    """The skill's `auto`-mode attachments, which a call does not apply, as a
    `{code, message}` warning — or None. Read from the stored list, which
    holds each attachment's mode (the schema's default, `auto`, included)."""
    ids = [
        str(kb.get("knowledge_base_id"))
        for kb in knowledge_bases
        if isinstance(kb, dict) and kb.get("mode", AUTO) == AUTO
    ]
    if not ids:
        return None
    return {
        "code": "knowledge_mode_unsupported",
        "message": (
            "A knowledge base attached to a skill in 'auto' mode is not applied: "
            "auto retrieval is built with the call, before any skill is loaded. "
            "Attach it in 'tool' mode (searched through query_knowledge once the "
            "skill is loaded) or 'prompt' mode (its documents join the skill's "
            "instructions)."
        ),
        "modes": [AUTO],
        "knowledge_base_ids": ids,
    }


async def resolve_skill_knowledge(
    session: AsyncSession,
    project_id: UUID,
    skill: str,
    knowledge_bases: Sequence[Any],
) -> SkillKnowledge:
    """`knowledge_bases` of the skill named `skill` as a call applies them.

    Best-effort, like the agent's own KBs: an attachment that does not parse,
    a KB that is no longer the project's, an `auto` one, or a failed read is a
    warning and the skill loads without it.
    """
    attachments = _attachments(skill, knowledge_bases)
    if not attachments:
        return NO_KNOWLEDGE
    from turncall.services.retrieval import get_full_text_context
    from turncall.storage.repositories import knowledge_repo

    try:
        usable = []
        for att in attachments:
            kb = await knowledge_repo.get_knowledge_base_by_id(
                session, att.knowledge_base_id, project_id=project_id
            )
            if kb is None:
                logger.warning(
                    "skill_knowledge_base_skipped: {kb} of skill {skill} is not a "
                    "knowledge base in the project",
                    kb=str(att.knowledge_base_id),
                    skill=skill,
                )
                continue
            usable.append(att)
        prompt_ids = [a.knowledge_base_id for a in usable if a.mode == "prompt"]
        text = await get_full_text_context(session, prompt_ids) if prompt_ids else ""
    except Exception:
        logger.exception(
            "skill_knowledge_skipped: could not read the knowledge bases of {skill}",
            skill=skill,
        )
        return NO_KNOWLEDGE
    search = tuple(a.model_dump() for a in usable if a.mode == "tool")
    return SkillKnowledge(text=text, search=search)


def _attachments(
    skill: str, knowledge_bases: Sequence[Any]
) -> list[KnowledgeBaseAttachment]:
    """The attachments a call can apply, by priority, each KB once."""
    parsed: list[KnowledgeBaseAttachment] = []
    for raw in knowledge_bases:
        try:
            parsed.append(KnowledgeBaseAttachment.model_validate(raw))
        except ValidationError:
            logger.warning(
                "skill_knowledge_base_skipped: an attachment of skill {skill} "
                "does not parse",
                skill=skill,
            )
    auto = auto_mode_warning([a.model_dump(mode="json") for a in parsed])
    if auto is not None:
        logger.warning(
            "skill_knowledge_base_skipped: {skill}: {message}",
            skill=skill,
            message=auto["message"],
        )
    applied: list[KnowledgeBaseAttachment] = []
    seen: set[UUID] = set()
    for att in sorted(parsed, key=lambda a: a.priority):
        if att.mode == AUTO or att.knowledge_base_id in seen:
            continue
        seen.add(att.knowledge_base_id)
        applied.append(att)
    return applied

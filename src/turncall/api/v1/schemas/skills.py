"""Skill API schemas (#423).

A skill's tools are the agent's tool schema (`ToolDefinitionSchema`), so they
follow exactly an agent's rules: the outbound URL rule, a webhook required for
a non-built-in, the webhook secret's bounds. What is a skill's own: a refusal
names the tool it is about, and a built-in's name is refused, since a skill
tool is a webhook tool and built-ins outrank every skill tool.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidatorFunctionWrapHandler,
    computed_field,
    field_validator,
)

from turncall.api.v1.schemas.agents import (
    InlineKnowledgeBaseSchema,
    ToolDefinitionSchema,
)
from turncall.domain.config_secrets import sanitize_tools
from turncall.domain.models import (
    BUILTIN_TOOL_NAMES,
    SKILL_NAME_MAX,
    SKILL_NAME_PATTERN,
)
from turncall.services.skill_knowledge import auto_mode_warning
from turncall.services.skills import pin_refusal

# The description is shown to the model to choose a skill by, as a tool's is;
# the instructions join the prompt, bounded as an agent's system prompt is.
DESCRIPTION_MAX = 1024
INSTRUCTIONS_MAX = 128000


def _tool_label(raw: Any, index: int) -> str:
    name = None
    if isinstance(raw, list) and index < len(raw) and isinstance(raw[index], dict):
        name = raw[index].get("name")
    return f"tools[{index}] {name!r}" if name else f"tools[{index}]"


def _named(raw: Any, exc: ValidationError) -> str:
    """Each error as `tools[1] 'get_invoices' webhook_url: <why>`."""
    lines = []
    for error in exc.errors():
        loc, why = error["loc"], error["msg"].removeprefix("Value error, ")
        if not loc or not isinstance(loc[0], int):
            lines.append(why)
            continue
        where = " ".join(
            [_tool_label(raw, loc[0]), ".".join(str(part) for part in loc[1:])]
        ).strip()
        lines.append(f"{where}: {why}")
    return "; ".join(lines)


def _check_tools(tools: list[ToolDefinitionSchema]) -> list[ToolDefinitionSchema]:
    names = [t.name for t in tools]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        msg = f"tool names must be unique within a skill: {', '.join(duplicates)}"
        raise ValueError(msg)
    builtins = sorted(set(names) & BUILTIN_TOOL_NAMES)
    if builtins:
        msg = (
            f"tool {builtins[0]!r} is a built-in: a skill's tools are webhook "
            "tools, and a built-in outranks every skill tool"
        )
        raise ValueError(msg)
    return tools


def _check_knowledge_bases(
    kbs: list[InlineKnowledgeBaseSchema],
) -> list[InlineKnowledgeBaseSchema]:
    ids = [str(kb.knowledge_base_id) for kb in kbs]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        msg = f"knowledge base attached twice: {', '.join(duplicates)}"
        raise ValueError(msg)
    return kbs


class _SkillContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("tools", mode="wrap", check_fields=False)
    @classmethod
    def _tools_by_name(cls, value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
        if value is None:
            return None
        try:
            tools = handler(value)
        except ValidationError as exc:
            raise ValueError(_named(value, exc)) from None
        return _check_tools(tools)

    @field_validator("knowledge_bases", check_fields=False)
    @classmethod
    def _unique_knowledge_bases(cls, value: Any) -> Any:
        return None if value is None else _check_knowledge_bases(value)


class CreateSkillRequest(_SkillContent):
    """A new draft: version 1 of a new name, or the next version of one the
    project has."""

    name: str = Field(
        ..., min_length=1, max_length=SKILL_NAME_MAX, pattern=SKILL_NAME_PATTERN
    )
    description: str = Field(..., min_length=1, max_length=DESCRIPTION_MAX)
    instructions: str = Field(..., min_length=1, max_length=INSTRUCTIONS_MAX)
    tools: list[ToolDefinitionSchema] = Field(default_factory=list)
    knowledge_bases: list[InlineKnowledgeBaseSchema] = Field(default_factory=list)


class UpdateSkillRequest(_SkillContent):
    """A draft's content; a field left out is kept. The name is not here: it
    is what the versions share."""

    description: str | None = Field(
        default=None, min_length=1, max_length=DESCRIPTION_MAX
    )
    instructions: str | None = Field(
        default=None, min_length=1, max_length=INSTRUCTIONS_MAX
    )
    tools: list[ToolDefinitionSchema] | None = None
    knowledge_bases: list[InlineKnowledgeBaseSchema] | None = None

    def values(self) -> dict[str, Any]:
        """The columns to write: each field sent and not null, as stored."""
        return {
            name: self.model_dump(mode="json", include={name})[name]
            for name in sorted(self.model_fields_set)
            if getattr(self, name) is not None
        }


class SkillResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    project_id: UUID
    name: str
    version: int
    state: str
    description: str
    instructions: str
    tools: list[dict[str, Any]]
    knowledge_bases: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None

    # Derived on every read, never stored: the agent read's shape (#148). An
    # `auto`-mode knowledge base is accepted and not applied on a call (#429).
    @computed_field  # type: ignore[prop-decorator]
    @property
    def warnings(self) -> list[dict[str, Any]]:
        auto = auto_mode_warning(self.knowledge_bases)
        return [auto] if auto is not None else []

    @classmethod
    def from_row(cls, row: Any) -> "SkillResponse":
        return cls(
            id=row.id,
            project_id=row.project_id,
            name=row.name,
            version=row.version,
            state=row.state,
            description=row.description,
            instructions=row.instructions,
            tools=sanitize_tools(row.tools or []),
            knowledge_bases=row.knowledge_bases or [],
            created_at=row.created_at,
            updated_at=row.updated_at,
            published_at=row.published_at,
        )


class SkillAgentResponse(BaseModel):
    """One agent version that lists a skill (#428), and the skill version it
    resolves to now: for an entry that pins one (#426), that version — None
    if it no longer runs (deleted, or not a version of this skill) — else the
    live published one, or None with nothing published."""

    model_config = ConfigDict(frozen=True)

    agent_id: UUID
    agent_name: str
    agent_version: int
    agent_state: str
    skill: str
    resolved_skill_id: UUID | None
    resolved_version: int | None
    pinned: bool = False

    @classmethod
    def from_rows(
        cls,
        agent: Any,
        skill: str,
        live: Any | None,
        versions: Mapping[UUID, Any] | None = None,
    ) -> "SkillAgentResponse":
        """`versions`: the project's skill versions the listed agents pin,
        by id."""
        pin = pinned_version_id(agent.config_blob, skill)
        resolved = live
        if pin is not None:
            resolved = (versions or {}).get(pin)
            if resolved is not None and pin_refusal(skill, resolved) is not None:
                resolved = None
        return cls(
            agent_id=agent.id,
            agent_name=agent.name,
            agent_version=agent.version,
            agent_state=agent.state,
            skill=skill,
            resolved_skill_id=resolved.id if resolved is not None else None,
            resolved_version=resolved.version if resolved is not None else None,
            pinned=pin is not None,
        )


def pinned_version_id(config_blob: Any, skill: str) -> UUID | None:
    """The version an agent's stored config pins `skill` to, or None."""
    entries = config_blob.get("skills") if isinstance(config_blob, dict) else None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("skill") == skill:
            raw = entry.get("version_id")
            try:
                return UUID(str(raw)) if raw else None
            except ValueError:
                return None
    return None

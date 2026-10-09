"""Campaign API schemas (#229)."""

from datetime import datetime
from functools import cache
from typing import Any
from uuid import UUID
from zoneinfo import available_timezones

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from turncall.api.v1.schemas import E164_PATTERN
from turncall.api.v1.schemas.phone_numbers import RoutingWeightEntry
from turncall.services.weighted_routing import pick_agent_by_weight
from turncall.storage.models import CampaignRow, CampaignTargetRow

# One request carries the whole list, so it is bounded like any request body.
MAX_TARGETS = 10_000

Variable = str | int | float | bool


@cache
def _iana_names() -> frozenset[str]:
    return frozenset(available_timezones())


def _iana(value: str | None) -> str | None:
    """An IANA zone name, exactly. Never inferred from a number: a ported
    mobile keeps its area code wherever its owner lives."""
    if value is not None and value not in _iana_names():
        raise ValueError(f"{value!r} is not an IANA timezone (e.g. 'America/New_York')")
    return value


class CampaignTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str = Field(..., pattern=E164_PATTERN)
    # Rendered into the agent's prompt, first message and tool descriptions as
    # `{{name}}`, the way call-init's `variables` are.
    variables: dict[str, Variable] = Field(default_factory=dict)
    timezone: str | None = None

    _timezone = field_validator("timezone")(_iana)


class CreateCampaignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=255)
    # Exactly one of these: one agent for every target, or weights (summing to
    # 100) that split the targets between agents, each by its number (#234).
    agent_id: UUID | None = None
    agents: list[RoutingWeightEntry] | None = Field(None, min_length=2, max_length=10)
    from_number_id: UUID
    # The timezone of every target that names none.
    timezone: str | None = None
    targets: list[CampaignTargetRequest] = Field(
        ..., min_length=1, max_length=MAX_TARGETS
    )

    _timezone = field_validator("timezone")(_iana)

    @model_validator(mode="after")
    def _one_way_to_pick_agents(self) -> "CreateCampaignRequest":
        if (self.agent_id is None) == (self.agents is None):
            raise ValueError("give exactly one of agent_id and agents")
        if self.agents is not None:
            total = sum(a.weight for a in self.agents)
            if total != 100:
                raise ValueError(f"agents' weights must sum to 100 (got {total})")
        return self

    @model_validator(mode="after")
    def _every_target_placed(self) -> "CreateCampaignRequest":
        """A timezone for every target, and no number twice.

        Refused rather than defaulted: a target's local time is what its
        calling window is read in, and a guessed zone calls people at night.
        """
        if self.timezone is None:
            missing = [i for i, t in enumerate(self.targets) if t.timezone is None]
            if missing:
                raise ValueError(
                    f"targets {missing[:10]} have no timezone and the campaign "
                    "sets no default: give each target one, or the campaign one"
                )
        seen: set[str] = set()
        for target in self.targets:
            if target.number in seen:
                raise ValueError(f"{target.number} is listed twice")
            seen.add(target.number)
        return self

    def agent_ids(self) -> list[UUID]:
        """Every agent this campaign may dial as."""
        if self.agents is not None:
            return [a.agent_id for a in self.agents]
        return [self.agent_id] if self.agent_id is not None else []

    def agent_weights(self) -> list[dict[str, Any]] | None:
        """The weights as a phone number's `routing_weights` stores them."""
        if self.agents is None:
            return None
        return [{"agent_id": str(a.agent_id), "weight": a.weight} for a in self.agents]

    def resolved_targets(self) -> list[dict[str, Any]]:
        """Each target with its own timezone, else the campaign's, and its
        agent — the weights' pick by its number, the phone-number A/B rule, so
        one number always draws the same agent from the same weights."""
        weights = self.agent_weights()
        return [
            {
                "number": t.number,
                "variables": {k: str(v) for k, v in t.variables.items()},
                "timezone": t.timezone or self.timezone,
                "agent_id": (
                    pick_agent_by_weight(weights, t.number)
                    if weights
                    else self.agent_id
                ),
            }
            for t in self.targets
        ]


class CampaignResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    name: str
    # One of the two: a single agent, or `[{agent_id, weight}]`.
    agent_id: UUID | None
    agents: list[dict[str, Any]] | None
    from_number_id: UUID
    timezone: str | None
    status: str
    # Targets per status, every status present.
    targets: dict[str, int]
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    @classmethod
    def from_row(cls, row: CampaignRow, counts: dict[str, int]) -> "CampaignResponse":
        return cls(
            id=row.id,
            name=row.name,
            agent_id=row.agent_id,
            agents=row.agent_weights,
            from_number_id=row.from_number_id,
            timezone=row.timezone,
            status=row.status,
            targets=counts,
            created_at=row.created_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )


class CampaignTargetResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    position: int
    number: str
    variables: dict[str, str]
    timezone: str
    # The agent that calls this target (#234). Null only on a target from
    # before weighted campaigns, which is called by the campaign's `agent_id`.
    agent_id: UUID | None
    status: str
    attempts: int
    # The latest attempt's call, and how it ended once it has.
    call_id: UUID | None
    ended_reason: str | None
    # Why a target was not dialled (`failed`, `suppressed`).
    error: str | None
    next_attempt_at: datetime | None
    updated_at: datetime

    @classmethod
    def from_row(cls, row: CampaignTargetRow) -> "CampaignTargetResponse":
        return cls(
            id=row.id,
            position=row.position,
            number=row.e164_number,
            variables=row.variables,
            timezone=row.timezone,
            agent_id=row.agent_id,
            status=row.status,
            attempts=row.attempts,
            call_id=row.call_id,
            ended_reason=row.ended_reason,
            error=row.error,
            next_attempt_at=row.next_attempt_at,
            updated_at=row.updated_at,
        )

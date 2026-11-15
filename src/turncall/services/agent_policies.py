"""Agent acceptance's policy table (#474): one row per origin, saying what
`services/agent_acceptance` refuses and what it warns about for a config
from there."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class AgentOrigin(StrEnum):
    """Where an agent config came from. Closed: a new entry point is a new row."""

    API = "api"  # agent create and update
    CALL_INIT = "call_init"  # a call-init answer, on any transport
    HANDOFF = "handoff"  # a handoff's target
    EVAL_TARGET = "eval_target"  # an eval run's `agent` / `agent_name` target
    EVAL_INLINE = "eval_inline"  # an eval run's `inline` target
    DEMO = "demo"  # the website demo's agents file
    STORED_CALL = "stored_call"  # a number routed to a stored agent


class UnknownFields(StrEnum):
    """What a top-level key the config schema does not know becomes, in a
    raw config. A stored agent's blob (an agent id, on any origin) is never
    checked: the schema wrote it, so an unknown key there is one a later
    release removed, nothing to refuse or warn about."""

    REFUSE = "refuse"
    WARN = "warn"  # warned about, then dropped by the domain parse
    # Not checked: an origin that only ever reads a stored agent, by id.
    UNCHECKED = "unchecked"


@dataclass(frozen=True)
class OriginPolicy:
    """One origin's refuse-or-warn choices.

    Provider rules and the procedures graph rule are refused on every origin,
    and an agent id must be one of the project's wherever one is taken; those
    are not choices, so they are not columns.

    The project checks — an ambience sound, the skills listed and a
    procedure's handoff targets must be the project's — are the `api` row's
    alone (`project_checks`), in the order the agent endpoints ran them.
    Every other origin keeps what it did before: the call resolves each
    against its project, and a call-init agent's skill that does not resolve
    is a warning on the call, never a refusal, by design (#431).
    """

    unknown_fields: UnknownFields
    # Plain `http://` outbound URLs (#340): refused at the API, allowed on
    # every inline config. A stored config was checked when it was saved.
    plain_http: bool
    # Whether the source may be an agent id rather than a raw config.
    takes_agent_id: bool
    # Run the API's whole `AgentConfigSchema` (its allowlists, required
    # models, strict nested sections): agent create and update (#478), and
    # `eval_inline` and `demo`, as each did before acceptance (#479). A
    # refusal keeps the schema's own errors (`schema_errors`), which the
    # agent endpoints answer as their 422, and the config that runs is the
    # one a save would store.
    full_schema: bool = False
    # Whether `knowledge_bases` may name the config's KBs. Not on a config
    # saved as an agent row, whose KBs are its links — one source, never two
    # that can disagree (#260). Checked with `full_schema` only.
    inline_knowledge_bases: bool = True
    # Check what the config names against the project (its ambience sound,
    # skills and procedure handoff targets; `agent_project_checks`): only
    # where it is saved as an agent row, at the API. Every other origin
    # leaves them to the call, which resolves each against its own project —
    # a call-init agent's skill that is not there is a warning, by design
    # (#431).
    project_checks: bool = False


POLICIES: Mapping[AgentOrigin, OriginPolicy] = {
    AgentOrigin.API: OriginPolicy(
        UnknownFields.REFUSE,
        plain_http=False,
        takes_agent_id=False,
        full_schema=True,
        inline_knowledge_bases=False,
        project_checks=True,
    ),
    AgentOrigin.CALL_INIT: OriginPolicy(
        UnknownFields.WARN, plain_http=True, takes_agent_id=True
    ),
    # A stored config was written through the schema, so its keys are not
    # checked (`UnknownFields`).
    AgentOrigin.HANDOFF: OriginPolicy(
        UnknownFields.UNCHECKED, plain_http=True, takes_agent_id=True
    ),
    # A stored config, like a handoff's; the eval looks the row up itself.
    AgentOrigin.EVAL_TARGET: OriginPolicy(
        UnknownFields.UNCHECKED, plain_http=True, takes_agent_id=True
    ),
    AgentOrigin.EVAL_INLINE: OriginPolicy(
        UnknownFields.REFUSE,
        plain_http=True,
        takes_agent_id=False,
        full_schema=True,
    ),
    AgentOrigin.DEMO: OriginPolicy(
        UnknownFields.REFUSE,
        plain_http=True,
        takes_agent_id=False,
        full_schema=True,
    ),
    AgentOrigin.STORED_CALL: OriginPolicy(
        UnknownFields.UNCHECKED, plain_http=True, takes_agent_id=True
    ),
}

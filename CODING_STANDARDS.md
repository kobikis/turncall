# Coding Standards

The rules a change to the TurnCall engine is reviewed against. `/code-review`
reads this file for its Standards axis, and so do human reviewers. Each rule
says why it exists; where a test enforces it, the test is named. A rule with no
test is enforced in review.

`CLAUDE.md` maps the components, `CONTEXT.md` holds the vocabulary and `adr/`
records the decisions. Read the ADR that covers an area before changing it.

---

Pipecat is imported only under `src/turncall/orchestrator/`. That boundary is
what keeps a pipecat upgrade (ADR-0028) a contained change. A service, route or
repository that needs something from a pipeline asks an orchestrator function
for it.

```python
# BAD: in src/turncall/services/
from pipecat.frames.frames import EndFrame

# GOOD: the frame is built in orchestrator/; the service calls a plain
# function exported from there and never sees a pipecat type
```

---

A rule that refuses a config is written once and applied in two places: at the
API, as a 422, and at pipeline build, as `pipeline_error` with `error.raised`. A
call-init inline agent, an eval `inline` target and a failover member never pass
the API schema, so a check that only runs at the API never sees them. Examples:
`xai_config.llm_refusal`, `deepgram_flux.mixes_flux` and
`procedure_graph.procedures_refusal`.

```python
# BAD: the check exists only in the Pydantic validator
@model_validator(mode="after")
def _no_base_url(self): ...

# GOOD: one pure function, called from the validator and from the build
def llm_refusal(llm: LLMSettings) -> str | None: ...
```

---

Every call event and webhook goes out through `events/emit.py`. Use
`emit_call_event`, `record_call_event`, `dispatch_call_event` or
`dispatch_project_event`. Redaction (ADR-0024) can only work at a single exit
point. Text reaches an event only as an `emit.Redacted`, built by
`await emit.redact(...)` before the transaction opens. A plain dict carries ids,
numbers and structure only. When you add a field that is an identifier, list it
in `redaction._STRUCTURAL_FIELDS`. Enforced by `tests/unit/test_event_exit_point.py`.

---

A mutating route under `/v1/agents`, `/v1/skills`, `/v1/credentials`,
`/v1/phone-numbers`, `/v1/api-keys` or `/v1/projects` takes `audit: Audit` and
records once per resource it changed. Wrap the write in `audit.change(...)`;
never snapshot a "before" by hand. A commit with no entry is refused at
runtime. Enforced by `tests/unit/test_audit_wiring.py`.

---

A provider key is resolved through `services/credentials.Credentials`, in the
order agent inline, then project credential, then platform env. No code reads
`os.environ` for a key. Reading the env directly skips the project's own
credential (ADR-0020), and a project that brought its own key would be billed to
the platform.

---

A voice edge opens its call through `services/call_opening.open_call` and runs
it through `orchestrator/call_runner.run_call` (ADR-0034). It keeps only its
protocol: the signature check, parsing, the answer format and its transport
factory. Opening owns agent acceptance, the live-call slot, the call row and its
first events; running owns the drain count, the slot's hold, MCP, the ambience
mixer, the call context, the build and the start. An edge that does any of those
steps itself fails `test_voice_edges_use_the_call_module.py`; what each step
does is tested through the modules (`test_call_opening.py`, `test_run_call.py`).

A new carrier adds itself to `CARRIERS` and `CARRIER_MEDIA` (`test_carrier_boundary.py`).

---

An inline agent has no agent row. Its `CallContext` uses `DYNAMIC_AGENT_ID` and
never writes it to `active_agent_id`. The agent's config lives in
`metadata_json["dynamic_config"]`, and post-call code reads it only through
`call_analysis_trigger.config_for_call()`. Dropping either one is silent: the
call ends before its pipeline starts, or `call.ended` is never sent. Enforced by
`test_inline_agent_context.py` and `test_call_opening.py`.

---

A change that swaps an agent's pipeline state (a handoff, a skill load, a
procedure move) is prepare-then-apply. All I/O happens and every frame is built
before the commit. After the commit, the apply step has nothing that suspends
and nothing that can raise: only computation and non-suspending queue puts. A
suspension point there reopens the window a barge-in cancels through, which
leaves the database and the live pipeline disagreeing (#144). Enforced by
`test_handoff_switch_is_atomic.py`.

```python
# BAD: a round-trip after the commit
await call_control.handoff_to_agent(...)
target = await load_agent(...)          # cancelled here = half-switched call

# GOOD
prepared = await _prepare_handoff(...)  # all I/O, all frames
await call_control.handoff_to_agent(...)
await _apply_handoff_context(prepared, ...)  # never suspends, cannot raise
```

---

Never swallow `CancelledError`. A broad `except Exception` does not catch it,
and that is what you want: a cancelled call must stop. Code that must finish
despite cancellation (recording `call.transferred`, a goodbye) is shielded on
purpose, as `tool_bridge` does, and carries a comment saying why.

---

A workaround for a pipecat bug, or a read of one of its private functions, comes
with a test that fails once it stops being needed (`test_pipecat_still_needs_it`)
or once the private function moves (`test_pipecat_parser_contract.py`). A
workaround with no such test outlives its reason.

---

Outbound URLs pass two different gates. Do not merge them.
- `services/url_allowlist.check_url_allowed` is the SSRF gate (`BYOM_ALLOWED_URL_PATTERNS`). It matches the whole URL and the host separately.
- `services/outbound_url.check_outbound_url` is the https rule (`ALLOW_PLAIN_HTTP_URLS`).

A new field that holds a URL TurnCall will call goes through both.

---

A stored secret is sealed under `CREDENTIAL_ENCRYPTION_KEY` and masked on every
read. A secret field that comes back as the mask (`***`) keeps the stored value,
and it is never written back as `***`. A new secret field in agent config is
added to `config_secrets`, and its masked form to the audit state.

---

Domain models are frozen (`@dataclass(frozen=True)`, frozen Pydantic). State
changes by building a new value. `ProcedureState` and `CallSkills` are replaced
in one assignment. Mutating them in place breaks the single-assignment swap the
rule above depends on.

---

A setting the user can't see fail is a warning they can read, not a log line.
Use `warnings` on the agent read, a run warning on an eval, or `error.raised` on
a call. Refuse at the API (422) only what is wrong in every deployment. Warn
about what depends on the deployment or on a later choice: the TTS voice, the
transport, whether a key is present.

---

Comments explain why, and name the issue or measurement behind a number:
`# measured 2026-10-31: one request in 10-20 stalls 30s+`. A constant that came
from a measurement says so. Don't write comments that repeat what the code says.

---

Optional parameters are scrutinised. Most bugs here come from a caller that
omitted a parameter and got the default. Prefer keyword-only parameters
(`*, shared: bool`) with no default when the caller must decide.

---

Never write a competitor's product name in code, comments, docs, issues, PRs or
commit messages.

## Testing

`make test` is hermetic. It needs no network and no provider keys, and it
excludes the `live` marker. Tests that need Postgres self-skip when it is
unreachable, so run `make docker-up && make migrate` before you trust a green
run on anything that touches the database.

Test behaviour through the public seams: the REST API, `tests/harness/` (the
real app on stub providers and a fake carrier) and the domain functions.
Mock at system boundaries only: providers, carriers and the clock. Never mock
TurnCall's own modules. A test that would break under a refactor that keeps the
behaviour is testing the wrong thing.

A structural rule (a boundary, a single exit point, a wiring every entry point
needs) gets a test that walks the code or the routes rather than a list. That
way a module added later is covered the day it is added. The `*_wiring.py` and
`*_boundary.py` tests are the model.

Work one test at a time, RED then GREEN, as vertical slices. Don't write all the
tests first. Never delete a test to make a change pass; port its cases first.

Tests that need real providers go under `tests/live/` with `@pytest.mark.live`.
They skip when credentials are missing.

## New-file budget

These are reviewer-enforced; CI doesn't measure them.
- A new file stays under 400 lines.
- A function stays under 50 lines and under 4 levels of nesting.
- Five files are already over 800 lines; `pipeline_factory.py` is the largest. Don't add to them: extract first, in a separate refactor commit.

New code is typed and `ruff format`-clean even though neither is gated. Don't
run `ruff format` or `mypy` fixes repo-wide in a feature PR, because they bury
the diff.

## Before you open a PR

- `make lint && make test` pass.
- `make check-openapi` passes. After an API change, run `make gen-openapi` and commit the result.
- Every commit is `git commit -s`, and every message is a conventional commit.
- The PR body says what a self-hoster would notice (a default, an env var, a response shape), since that text becomes the changelog entry. Add a row to `CLAUDE.md` when you introduce a new env var.
- A change the Helm chart needs gets a PR in `kobikis/turncall-charts`.

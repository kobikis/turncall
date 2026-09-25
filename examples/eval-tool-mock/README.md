# Eval Tool Mock Example

An eval that exercises "book the table" against an agent whose booking webhook
points at a host that does not exist — and passes. That is the demonstration:
the mock intercepts the call before the webhook, so the scenario tests the
agent's behaviour without reserving anything.

## What it does

- **Creates an agent with a webhook tool** whose URL is `https://book.invalid/reserve`
  — unroutable by design (RFC 2606). If a mock ever failed to intercept, the run
  would fail against that host, so a green run is evidence rather than a claim
- **Runs the same conversation three times, with three different mocks**:
  - `booking-confirmed` — the tool returns a reference, and the agent must repeat it
  - `booking-refused` — the tool refuses, and the agent must **not** claim a table
  - `no-mock-at-all` — nothing answers for the tool, so the run is `errored`
- **Shows why `mock_only` is the default**: an eval suite with live tools books a
  real table on every iteration, of every run, every night

## Prerequisites

1. **Docker** (Postgres + Redis + the API + the eval worker)
2. **OpenAI API key** in `.env` — the agent's LLM
3. **PLATFORM_API_KEY** in `.env` — project and key creation are platform-gated

No phone number and no tunnel: evals never touch telephony.

## Quick Start

```bash
make docker-up-local && make migrate-local
./examples/eval-tool-mock/run.sh
```

Expect three verdicts, in about a minute:

```
booking-confirmed: passed (1/1)
booking-refused:   passed (1/1)
no-mock-at-all:    errored (0/1) — unmocked tool: book_table
```

## The three rules it demonstrates

**A mock belongs to the scenario, not the run.** "The booking succeeds" and "the
booking fails" are two tests with the same turns, which is why the example stores
two scenarios rather than one with a switch. The failing branch is the one worth
writing: *claims a table it does not have* is the expensive bug, and only the
refused mock can catch it.

**`errored` is not `failed`.** The third run proves nothing about the agent —
nobody let it try — so it counts toward neither rate and reads as "could not
check". A suite that scored it `failed` would send someone hunting a regression
that never happened.

**Fail closed.** Under `mock_only` a tool with no mock is refused, not executed.
`tool_policy: "live"` is the typed opt-in, and it is for read-only lookups.

## Notes

- The `function_call` expectation takes a **`calls:` list**, not top-level
  `name`/`args` — the parser ignores keys it does not know, so the wrong shape
  parses cleanly and asserts nothing.
- **An eval connects no MCP servers.** A mock naming an MCP tool never fires,
  because the model is never shown that tool; the run warns rather than failing.
  Webhook tools, like the one here, and built-ins are mocked normally.
- The scenarios are left behind for poking at:
  `GET /v1/eval-scenarios` with the API key the script prints.

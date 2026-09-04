# Contributing to TurnCall

Thanks for considering it. This document is deliberately specific about what
gets merged, so you don't spend a weekend on something we'd decline.

## Before you write code

**Bug fixes and documentation: just open a PR.** No discussion needed.

**Features: open an issue first.** Describe what you want and why. Wait for a
maintainer to agree on the approach before writing code. TurnCall is a small
project with a roadmap, and unsolicited feature PRs are the ones most likely to
sit unreviewed or get closed — not because they're bad, but because they don't
fit a direction you couldn't have known about.

Good first contributions: provider integrations that follow an existing pattern
in `src/turncall/orchestrator/pipeline_factory.py`, documentation gaps you hit
while self-hosting, and the type-checking debt described below.

## Developer Certificate of Origin

Contributions are accepted under the [DCO](https://developercertificate.org/) —
you certify you wrote the code, or otherwise have the right to submit it under
the project's MIT license. There is no CLA for TurnCall.

Sign off each commit:

```bash
git commit -s -m "fix: ..."
```

That appends `Signed-off-by: Your Name <your@email>`. Set `user.name` and
`user.email` in git first. If you forget, `git commit --amend -s` fixes the
last one.

## Setting up

```bash
make dev           # install with dev extras
make docker-up     # Postgres + Redis + API on :8090 + LocalStack
make migrate       # create tables (needs docker-up first)
make test          # 610 tests, ~5s
```

Most tests need nothing running: 609 of 610 pass with no Postgres. The one that
needs it (`tests/integration/test_call_event_seq_race.py`) **self-skips** when
the database is unreachable, so run `make docker-up && make migrate` before
trusting a green run on anything touching call events.

For a hot-reload server use `make run`, but stop the `turncall` container first
— both bind `:8090`.

## What CI enforces

Honest accounting, because a contract you can't verify is worse than none:

| Check | Gated? |
|---|---|
| `ruff check src/ tests/` | **yes** |
| `pytest` (610 tests) | **yes** |
| `bandit`, high severity | **yes** |
| `mypy` | **no** — see below |
| `ruff format` | **no** — see below |
| Coverage threshold | **no** |

**Known debt, and we'd welcome help with it:**

- `mypy src/turncall/` reports **224 errors across 49 files** under the
  `strict = true` in `pyproject.toml`. The config is aspirational; the code
  hasn't caught up. Incremental fixes (one module at a time) are very welcome.
  Please don't submit a single 49-file PR — it can't be reviewed.
- `ruff format` is not clean and would reformat files unrelated to any given
  change. Don't run it repo-wide in a feature PR; it buries your diff.

New code should be typed and formatted well even though nothing forces it.

## Style

`.claude/rules/` describes conventions this project's AI tooling follows. The
parts that matter for humans: prefer immutable data, keep files under ~800
lines, handle errors explicitly rather than swallowing them, and validate input
at system boundaries. Match the surrounding code.

Pipecat imports stay inside `src/turncall/orchestrator/`. No other module
imports Pipecat — that boundary is deliberate and load-bearing, since it's what
makes framework upgrades a contained change.

## Commits and PRs

Conventional commits: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`,
`perf:`, `ci:`.

In the PR description, say what changed and why. If it changes behavior a
self-hoster would notice — a default model, an env var, an API response — say
so explicitly; that text becomes the changelog entry.

## Architecture

See `CLAUDE.md` for the component map, `CONTEXT.md` for domain vocabulary, and
`adr/` for decisions and the reasoning behind them. Read the relevant ADR
before changing something it covers.

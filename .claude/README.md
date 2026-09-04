# Claude Code Infrastructure

This directory contains Claude Code infrastructure copied from the
claude-code-python-showcase source of truth.

## Installation Date
2026-04-06 12:35:13

## Components Installed

### Skills (12)
Pattern libraries copied from source `.claude/skills/`:
python-patterns, async-python-patterns, python-testing, tdd-workflow,
postgres-patterns, docker-patterns, deployment-patterns, security-review,
design-doc-mermaid, perplexity-deep-search, verification-loop, strategic-compact

### Agents (13)
Specialist agents copied from source `.claude/agents/`:
planner, architect, tdd-guide, code-reviewer, security-reviewer,
fastapi-specialist, aws-specialist, k8s-specialist, python-database-expert,
python-debugger, pipecat-expert, twilio-expert, vapi-expert

### Commands (12)
Slash commands copied from source `.claude/commands/`:
/pr, /plan, /tdd, /code-review, /build-fix, /test-coverage, /verify,
/update-docs, /orchestrate, /pipecat-rca, /create-subagent, /create-command

### Hooks & Scripts
- Shell/Python hooks in `.claude/hooks/`
- JS hook scripts in `.claude/scripts/hooks/`
- JS library modules in `.claude/scripts/lib/`

### Rules (14)
- 9 common rules in `.claude/rules/common/`
- 5 Python-specific rules in `.claude/rules/python/`

## Usage

### Activating Skills
Skills activate automatically based on intent patterns and file paths.

### Running Commands
- `/orchestrate` - Multi-agent workflow orchestration
- `/plan` - Create implementation plan
- `/tdd` - Test-driven development workflow
- `/code-review` - Code quality review
- `/pr` - Create pull request with summary
- `/verify` - Run verification checks
- `/test-coverage` - Analyze test coverage
- `/build-fix` - Troubleshoot build failures
- `/update-docs` - Update documentation
- `/pipecat-rca` - Hypothesis-driven debugging for Python bots
- `/create-subagent` - Create new agent definitions
- `/create-command` - Create new slash commands

### Using Agents
Agents are invoked via the Task tool based on routing rules in CLAUDE.md.

## Updating

To update components from the showcase:
```bash
./update_component.sh /path/to/this/project [skills|agents|commands|hooks|rules|all]
```

## Backup

Original files backed up in `.claude_backup/`

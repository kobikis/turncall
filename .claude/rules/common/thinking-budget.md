# Thinking Budget Taxonomy

## Directive Levels

Every agent and command specifies a thinking budget directive on its own line after the title heading. This controls the depth of reasoning applied to the task.

| Level | Directive | When to Use |
|-------|-----------|-------------|
| Light | `Think.` | Simple lookup, file reads, straightforward validation |
| Moderate | `Think hard.` | Basic analysis, code review for specific issues, configuration |
| Deep | `Think harder.` | Multi-step workflows, architecture review, security audits |
| Maximum | `Ultrathink.` | Complex reasoning, multi-agent orchestration, root cause analysis |

## Placement

The directive appears on its own line immediately after the `# Title` heading:

```markdown
# Agent or Command Title

Ultrathink.

You are an expert in...
```

## Selection Guidelines

- **Default to `Ultrathink.`** for commands (they orchestrate complex workflows)
- **Default to `Think harder.`** for agents (they focus on a single domain)
- Use `Think.` or `Think hard.` only for lightweight, high-frequency tasks
- When in doubt, go one level higher — deeper thinking costs less than wrong output

## Mapping to Agent Types

| Agent Category | Recommended Level |
|---------------|-------------------|
| Planner, Architect | `Ultrathink.` |
| Code Reviewer, Security Reviewer | `Think harder.` |
| TDD Guide, Database Expert | `Think harder.` |
| FastAPI, AWS, K8s Specialists | `Think harder.` |
| Pipecat, Twilio, Vapi Experts | `Think harder.` |
| Python Debugger | `Ultrathink.` |
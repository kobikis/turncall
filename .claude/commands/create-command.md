---
description: Create a new Claude Code slash command by analyzing requirements, researching documentation, and studying existing commands for consistency.
allowed-tools: Read, Write, Bash, Glob, Grep
argument-hint: [command purpose or description]
---

# Create New Command: $ARGUMENTS

Ultrathink.

## Objective

Create a new Claude Code slash command based on user requirements. This meta-command researches official documentation and analyzes existing commands in the project to ensure the new command is consistent and well-designed.

## Step 1: Understand User Goals

Analyze the user's request: **$ARGUMENTS**

Key questions to answer:
- What is the primary purpose of this command?
- What tools will it need to use?
- Is it read-only or does it modify files?
- What kind of workflow should it follow?
- What output should it produce?
- What level of thinking complexity is appropriate?

### Requirements Clarity Check

**IMPORTANT**: Before proceeding:

1. **Analyze $ARGUMENTS**: What information did the user provide?
2. **Assess clarity**: Do you have confident answers to the key questions above?
3. **Decision point**:
   - Requirements are clear → Proceed to Step 2
   - Requirements are vague → STOP and ask clarifying questions

**If requirements are unclear:**
- List the specific aspects that need clarification
- Ask targeted questions to fill gaps
- Do NOT make assumptions about critical design decisions
- Wait for user response before proceeding

**Clarity Examples:**

*Clear*: "Create a command that runs pytest with coverage, identifies uncovered lines, and suggests tests"
→ Purpose, tools, and workflow are all specified

*Unclear*: "Make a test command"
→ Ask: What kind of tests? What framework? Should it generate tests or just run them?

## Step 2: Research Command Creation

### Research 2a: Official Documentation

Use WebFetch (if available) or built-in knowledge about Claude Code slash commands:
- File format and frontmatter structure (`description`, `allowed-tools`, `argument-hint`)
- Variable usage (`$ARGUMENTS` for user input)
- Tool access patterns
- Best practices for command design

### Research 2b: Existing Commands Analysis

Study existing commands in the project:

1. List all commands: `ls .claude/commands/`
2. Read 2-3 existing commands to understand:
   - Frontmatter patterns
   - Thinking budget usage
   - Workflow organization
   - How they reference agents and skills
   - Output formatting
   - Error handling approaches
3. Check `.claude/rules/common/thinking-budget.md` for thinking level guidelines

**Existing command format in this project:**
```yaml
---
description: <One-line description of what the command does>
allowed-tools: <Comma-separated tool list> (optional)
argument-hint: <Placeholder text> (optional)
---
```

Key conventions:
- Commands reference agents by name (e.g., "Use the **planner** agent")
- Commands reference skills by path (e.g., "Load `.claude/skills/<name>/SKILL.md`")
- Python/pytest focus throughout
- Integration notes at the bottom pointing to related commands

## Step 3: Design the Command

### Design Decisions

1. **Purpose**: Clear statement of what the command does
2. **Name**: kebab-case (e.g., `my-command.md`)
3. **Thinking Budget**: Determine appropriate level:
   - `Think.` — Simple, straightforward tasks with clear steps
   - `Think hard.` — Tasks requiring some analysis or planning
   - `Think harder.` — Complex tasks with multiple considerations
   - `Ultrathink.` — Highly complex, multi-agent, or creative tasks (default)
4. **Workflow**: Step-by-step process
5. **Tools needed**: Minimum set required
6. **Agent integration**: Which specialist agents to invoke
7. **Output format**: What the user sees
8. **Edge cases**: How to handle potential issues

## Step 4: Write the Command

Create `.claude/commands/<name>.md` following this structure:

```markdown
---
description: <Concise description of what the command does>
allowed-tools: <Tools needed>
argument-hint: <Placeholder for user input>
---

# <Command Title>: $ARGUMENTS

<Thinking Budget directive>

## Objective
<Clear statement of purpose>

## Step 1: <First major step>
<Instructions>

## Step 2: <Second major step>
<Instructions>

[Additional steps as needed]

## Integration with Other Commands

After this command:
- Use `/next-command` for follow-up work
- Use `/related-command` if needed

## Related Agents

This command invokes the `<agent-name>` agent.
```

### Guidelines

- **Reference agents**: Point to agents in `~/.claude/agents/<name>.md`
- **Reference skills**: Point to skills in `.claude/skills/<name>/SKILL.md`
- **Python focus**: All examples use Python, pytest, FastAPI patterns
- **Integration notes**: Always include which commands pair well together
- **Safety**: Include guardrails for destructive operations (git push, file deletion)

## Step 5: Save and Integrate

### Save the Command

Write to `.claude/commands/<command-name>.md`

### Update Generator

Inform the user to add the command name to `commands_generator.py`:

```python
COMMAND_NAMES = [
    # ... existing commands ...
    "<new-command-name>",
]
```

### Provide Usage Instructions

**Command Created**: `/<command-name>`
**Location**: `.claude/commands/<command-name>.md`

**How to Use:**
```
/<command-name> <arguments>
```

**Testing Suggestion:**
- Provide a concrete test invocation
- Example: "Try: `/<command-name> <sample-argument>`"

## Important Notes

- Command names should be kebab-case (e.g., `my-command.md`)
- Descriptions should be concise but clear
- Only include tools that are actually needed in `allowed-tools`
- `$ARGUMENTS` captures everything after the command name
- Commands are versioned in git under `.claude/commands/`
- The `commands_generator.py` COMMAND_NAMES list controls which commands are installed to target projects

**Begin by analyzing the user's requirements for their new command.**
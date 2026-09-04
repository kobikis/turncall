---
description: Create a new subagent definition for specialized tasks with isolated context. Researches official docs and analyzes existing agents before generating.
allowed-tools: Read, Write, Bash, Glob, Grep
argument-hint: [agent purpose or description]
---

# Create New Subagent: $ARGUMENTS

Ultrathink.

## Objective

Create a specialized subagent definition based on user requirements. Subagents are powerful tools for delegating specific tasks to AI assistants with fresh context windows, keeping the main conversation clean while enabling focused, expert-level work.

This command researches official Claude Code documentation and analyzes existing agents in the project to ensure consistency.

## Step 1: Understand Subagent Requirements

Analyze the user's request: **$ARGUMENTS**

Key questions to answer:
- What is the primary purpose of this subagent?
- What specialized expertise or role should it have?
- Should it be invoked automatically (proactive) or manually?
- What tools will it need access to?
- Should it be read-only or able to modify files?
- What kind of output should it produce?
- Are there any specific methodologies or guidelines it should follow?

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

*Clear*: "Create a security auditor that scans Python code for SQL injection, XSS, and hardcoded credentials"
→ Purpose, expertise, and scope are all specified

*Unclear*: "Make a security subagent"
→ Ask: What kind of security? Code auditing? Infrastructure? Compliance? Should it be proactive or manual?

## Step 2: Research Subagent Patterns

### Research 2a: Official Documentation

Use WebFetch (if available) or built-in knowledge to understand:
- Subagent file format and YAML frontmatter structure
- Available configuration options (name, description, tools, model)
- Best practices for effective subagent design
- How to make subagents proactive vs manual
- Model selection guidelines

### Research 2b: Existing Agents Analysis

Analyze existing agents in the project:

1. List all agents: `ls .claude/agents/`
2. Read 2-3 existing agents to understand:
   - YAML frontmatter patterns (name, description, tools, model)
   - System prompt structure and style
   - Thinking budget usage
   - Tool selection strategies
   - Output format specifications
3. Check `.claude/rules/common/agents.md` for the agent registry
4. Check `.claude/rules/common/thinking-budget.md` for thinking level guidelines

**Existing agent format in this project:**
```yaml
---
name: <kebab-case-name>
description: "<Purpose>. Use PROACTIVELY when <trigger conditions>."
tools: Read, Write, Edit, Bash, Glob, Grep
model: sonnet
---
```

Key conventions:
- Tools are comma-separated (no brackets)
- Model is `sonnet` (default), `opus` (complex reasoning), or `haiku` (lightweight)
- Description is a quoted string with proactive trigger guidance
- Content is Python-focused with code examples and review checklists

## Step 3: Design the Subagent

### Design Decisions

1. **Name**: lowercase-with-hyphens (e.g., `code-reviewer`, `security-auditor`)
   - Check for conflicts with existing agents

2. **Description**: Clear purpose statement
   - Include "Use PROACTIVELY when..." if it should auto-invoke
   - Specify trigger conditions

3. **Tools** (minimum necessary):
   - Read-only analysis: `Read, Grep, Glob`
   - Code modification: add `Write, Edit`
   - Shell access: add `Bash`
   - External data: add `WebFetch` (if available)

4. **Model Selection**:
   - `sonnet` — Balanced performance (default for most agents)
   - `opus` — Complex reasoning (architecture, planning, deep analysis)
   - `haiku` — Fast, cost-effective (simple validation, formatting)

5. **Thinking Budget**:
   - `Think.` — Simple lookup/review tasks
   - `Think hard.` — Basic analysis or validation
   - `Think harder.` — Multi-step analysis workflows
   - `Ultrathink.` — Complex reasoning, deep analysis (default)

6. **System Prompt Structure**:
   - Role definition and core principles
   - Responsibilities list
   - Methodology / workflow
   - Code examples (Python-focused)
   - Anti-patterns to avoid
   - Review checklist

## Step 4: Write the Subagent File

Create `.claude/agents/<name>.md` following this structure:

```markdown
---
name: <agent-name>
description: "<Purpose>. Use PROACTIVELY when <conditions>."
tools: <comma-separated tools>
model: <sonnet|opus|haiku>
---

# <Agent Title>

<Thinking Budget directive>

You are an expert in <domain> specializing in <specifics>. Deep expertise in <key areas>.

## Core Principles

1. **Principle one** - Description
2. **Principle two** - Description
...

## <Domain-Specific Section>

### Pattern / Code Example
```python
# Python code examples relevant to the domain
```

## Anti-Patterns to Avoid

- **Pattern name** — why it's wrong and what to do instead

## Review Checklist

When reviewing <domain> code:
- [ ] Check item 1
- [ ] Check item 2
...
```

### File Creation Steps

1. Write the agent file to `.claude/agents/<name>.md`
2. Verify the file was created successfully

## Step 5: Integration

### Update Agent Registry

After creating the agent, inform the user to update:

1. **`.claude/rules/common/agents.md`** — Add to the appropriate section table
2. **`CLAUDE.md`** — Add to the Available Agents table
3. **`commands_generator.py`** — Not needed (agents are separate from commands)

### Provide Usage Guidance

**Subagent Created**: `<name>`
**Location**: `.claude/agents/<name>.md`

**How to Use:**

1. **Automatic** (if proactive): Activates when relevant tasks are detected
2. **Manual**: "Use the <name> agent to..." or via the Agent tool

**Testing Suggestion:**
- Provide a concrete test case relevant to the agent's domain
- Example: "Try asking: 'Use <name> to review <specific-file>'"

**Customization:**
- Edit `.claude/agents/<name>.md` to refine behavior
- Changes take effect on next agent invocation

## Important Notes

- **Context Separation**: Subagents work in isolated context windows — they start fresh each time
- **Tool Restrictions**: Only grant tools the subagent actually needs
- **Proactive vs Manual**: Include "PROACTIVELY" in description for automatic invocation
- **Python Focus**: All code examples should use Python patterns consistent with this project
- **Immutability**: Follow the project's immutability conventions in code examples
- **Version Control**: `.claude/agents/` is tracked in git

**Begin by analyzing the user's requirements for their new subagent.**
# Agent Orchestration

## Available Agents

Located in `~/.claude/agents/`:

### Core Workflow
| Agent | Purpose | When to Use |
|-------|---------|-------------|
| planner | Implementation planning | Complex features, refactoring |
| architect | System design + API design | Architectural decisions, API contracts |
| tdd-guide | Test-driven development | New features, bug fixes |
| code-reviewer | Python code review | After writing code |
| security-reviewer | Security analysis | Before commits |

### Stack-Specific
| Agent | Purpose | When to Use |
|-------|---------|-------------|
| fastapi-specialist | FastAPI framework | DI, Pydantic v2, middleware, WebSocket, auth |
| aws-specialist | AWS services | Lambda, SQS, IoT Core, RDS, S3, CloudWatch |
| k8s-specialist | Kubernetes & containers | Deployments, Helm, Dockerfile, HPA, probes |

### Voice AI & Telephony
| Agent | Purpose | When to Use |
|-------|---------|-------------|
| pipecat-expert | Pipecat pipelines | Voice AI pipelines, STT/LLM/TTS, processors, transports |
| twilio-expert | Twilio Voice | Voice API, Media Streams, TwiML, telephony webhooks |
| vapi-expert | Vapi integration | Webhook payloads, call data, assistant config |

### Database
| Agent | Purpose | When to Use |
|-------|---------|-------------|
| python-database-expert | PostgreSQL + SQLAlchemy + Alembic | Schema, queries, migrations, optimization |

### Debugging
| Agent | Purpose | When to Use |
|-------|---------|-------------|
| python-debugger | Root cause analysis | Hard bugs, memory leaks, async issues |

## Immediate Agent Usage

No user prompt needed:
1. Complex feature requests - Use **planner** agent
2. Code just written/modified - Use **code-reviewer** agent
3. Bug fix or new feature - Use **tdd-guide** agent
4. Architectural decision - Use **architect** agent
5. FastAPI endpoint work - Use **fastapi-specialist** agent
6. AWS service integration - Use **aws-specialist** agent
7. K8s manifests or Dockerfiles - Use **k8s-specialist** agent
8. Database work - Use **python-database-expert** agent
9. Voice AI pipelines - Use **pipecat-expert** agent
10. Twilio telephony - Use **twilio-expert** agent
11. Vapi webhooks/calls - Use **vapi-expert** agent

## Parallel Task Execution

ALWAYS use parallel Task execution for independent operations:

```markdown
# GOOD: Parallel execution
Launch 3 agents in parallel:
1. Agent 1: Security analysis of auth module
2. Agent 2: Performance review of cache system
3. Agent 3: Type checking of utilities

# BAD: Sequential when unnecessary
First agent 1, then agent 2, then agent 3
```

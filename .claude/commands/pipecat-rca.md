---
description: Iterative root cause analysis for Python bot issues using hypothesis-driven debugging. Scientific approach with instrumented logging, human-in-the-loop verification, and iterative hypothesis testing.
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
argument-hint: [issue description]
---

# Python Bot Root Cause Analysis: $ARGUMENTS

Ultrathink.

## Objective

Perform iterative hypothesis-driven root cause analysis for a Python bot issue (Pipecat, FastAPI, or any async Python service). This command follows a scientific debugging approach:

1. **Analyze** the issue and relevant code
2. **Hypothesize** the root cause
3. **Instrument** with diagnostic logs
4. **Observe** via terminal output when user tests
5. **Verify** hypothesis via logs
6. **Iterate** (if wrong) or **Fix** (if confirmed)
7. **Validate** fix works
8. **Commit** (with user approval)

Use the **pipecat-expert** agent for Pipecat-specific issues or **python-debugger** agent for general Python debugging.

---

## Phase 1: Environment Setup

### Step 1.1: Identify the Service

Locate the main entry point of the service being debugged:

1. Use `Glob` to find bot/service entry points: `**/*bot*.py`, `**/main.py`, `**/app.py`
2. Use `Grep` to find process startup patterns: `uvicorn`, `PipelineRunner`, `asyncio.run`
3. Determine the service port and startup command

### Step 1.2: Stop Existing Process

Kill any existing process on the identified port:

```bash
lsof -ti:<PORT> | xargs kill -9 2>/dev/null || true
```

### Step 1.3: Start the Service

Start the service in the background so logs can be monitored:

```bash
# Example patterns — adapt to actual project
cd <project_dir> && python -m <module>
# or
cd <project_dir> && uvicorn app:app --port <PORT>
# or
cd <project_dir> && uv run python bot.py
```

Run this as a background task to monitor logs throughout the session.

### Step 1.4: Verify Service is Running

Check startup logs for success indicators. Inform the user the service is ready for debugging.

---

## Phase 2: Issue Analysis

### Step 2.1: Understand the Issue

Parse the issue description from `$ARGUMENTS`.

If `$ARGUMENTS` is empty or unclear, ask the user:
> "Please describe the issue you're experiencing. What behavior are you seeing vs. what you expect?"

### Step 2.2: Identify Relevant Code

Based on the issue, search relevant files:

1. Use `Glob` and `Grep` to find relevant code paths
2. Focus on:
   - Pipeline structure and data flow (Pipecat)
   - Endpoint handlers and middleware (FastAPI)
   - Event handlers, observers, callbacks
   - Async patterns (await chains, task scheduling)
   - Frame types and processor order (Pipecat-specific)

3. Use specialist agents if needed:
   - **pipecat-expert** for pipeline/processor/frame issues
   - **fastapi-specialist** for endpoint/middleware issues
   - **python-debugger** for general Python issues

**Document findings** for hypothesis formation.

---

## Phase 3: Hypothesis Formation

### Step 3.1: Form Initial Hypothesis

Based on code analysis, form a hypothesis about the root cause.

**Hypothesis Template:**
```
HYPOTHESIS #[N]:
- What I think is happening: [description]
- Why I think this: [evidence from code]
- How to prove/disprove: [specific logs to add]
- Expected log output if TRUE: [what logs would show]
- Expected log output if FALSE: [what logs would show]
```

**Present the hypothesis to the user** and explain what you'll be looking for.

---

## Phase 4: Instrumentation

### Step 4.1: Add Diagnostic Logs

Add diagnostic logging statements to prove or disprove the hypothesis.

**For standard Python / FastAPI:**
```python
import logging

logger = logging.getLogger(__name__)

logger.info("[RCA] Request received: method=%s path=%s", request.method, request.url.path)
logger.info("[RCA] State before: %r", state)
logger.info("[RCA] Condition check: condition=%r result=%r", condition, result)
logger.info("[RCA] State after: %r", state)
```

**For Pipecat pipelines:**
```python
from loguru import logger

logger.info(f"[RCA] Frame received: {type(frame).__name__}")
logger.info(f"[RCA] Frame content: {frame}")
logger.info(f"[RCA] Buffer state: {self._buffer}")
logger.info(f"[RCA] Pipeline position: {self.__class__.__name__}")
```

**Guidelines:**
- Prefix ALL diagnostic logs with `[RCA]` for easy filtering
- Log BEFORE and AFTER key operations
- Log frame types, request data, state changes
- Log conditional branch decisions with `{var=}` syntax
- Use f-strings with `=` for clear output: `f"[RCA] {value=}"`

### Step 4.2: Save Changes

Save the instrumented files. Do NOT commit instrumentation.

### Step 4.3: Restart Service

Kill and restart to pick up changes:

```bash
lsof -ti:<PORT> | xargs kill -9 2>/dev/null || true
sleep 1
cd <project_dir> && <startup_command>
```

Inform the user: "Diagnostic logs added and service restarted. Ready for you to test."

---

## Phase 5: User Handover — Test the Issue

### Step 5.1: Hand Over to User

Ask the user:
> "Please recreate the issue. When done, tell me what happened."
>
> Options:
> - "I've recreated the issue"
> - "I couldn't recreate it"
> - "Something different happened"

**While waiting**: Monitor the background task output for `[RCA]` logs.

---

## Phase 6: Log Analysis

### Step 6.1: Collect Evidence

After user recreates the issue:

1. Check background task output for `[RCA]`-prefixed logs
2. Look for:
   - Data flow (what was processed, in what order)
   - State changes (buffers, flags, counters)
   - Missing events (expected but not seen)
   - Errors or exceptions
   - Unexpected values or types

### Step 6.2: Analyze Results

Compare actual log output against hypothesis expectations:

```
ANALYSIS:
- Expected if TRUE: [from hypothesis]
- Actual output: [from logs]
- Conclusion: CONFIRMED / DISCONFIRMED
- New evidence: [anything unexpected]
```

---

## Phase 7: Decision Point

### If Hypothesis DISCONFIRMED:

1. **Reset instrumentation** — restore only the files you instrumented:
   ```bash
   git checkout -- <instrumented_files>
   ```

2. **Analyze why it was wrong** — what did the logs reveal?

3. **Form new hypothesis** → return to **Phase 3**

4. Inform user: "Hypothesis #N was incorrect. Based on the logs, I now suspect [new theory]. Adding new diagnostic logs."

### If Hypothesis CONFIRMED:

1. **Document the root cause:**
   ```
   ROOT CAUSE CONFIRMED:
   - The issue is: [description]
   - It happens because: [explanation]
   - Evidence: [log output that proves it]
   ```

2. **Reset instrumentation:**
   ```bash
   git checkout -- <instrumented_files>
   ```

3. **Proceed to fix** → Phase 8

---

## Phase 8: Implement Fix

### Step 8.1: Design the Fix

Based on confirmed root cause:

1. Identify the exact code change needed
2. Consider edge cases and async implications
3. Ensure fix doesn't break other functionality

**Present fix plan to user** before implementing.

### Step 8.2: Apply the Fix

Make the necessary code changes using `Edit` tool.

**Guidelines:**
- Make minimal, targeted changes
- Don't refactor unrelated code
- Preserve existing patterns and style
- Follow immutability principles where applicable

### Step 8.3: Restart and Test

1. Restart the service
2. Ask the user:
   > "I've implemented the fix and restarted. Please test it."
   >
   > Options:
   > - "The fix works!"
   > - "The issue still exists"
   > - "New issue appeared"
   > - "Partially fixed"

---

## Phase 9: Final Verification

### If Fix NOT Working:

1. Check service logs for errors
2. Analyze what's still wrong
3. Either iterate on the fix or reset and form new hypothesis

### If Fix WORKS:

Proceed to Phase 10.

---

## Phase 10: Commit (with approval)

### Step 10.1: Request Commit Approval

Ask the user:
> "The fix has been verified. Would you like me to commit these changes?"

### Step 10.2: Create Commit (if approved)

```bash
git add <fixed_files>
git commit -m "fix: <brief description>

Root cause: <one-line explanation>
"
```

---

## Iteration Tracking

Track the debugging session:

```
SESSION LOG:
============
Issue: [original description]
Service: [entry point and port]

Iteration 1:
- Hypothesis: [...]
- Files instrumented: [...]
- Result: CONFIRMED / DISCONFIRMED
- Evidence: [...]

[Repeat for each iteration]

Resolution:
- Root cause: [...]
- Fix applied: [file:line — description]
- Verified: YES / NO
- Committed: YES / NO
```

---

## Framework-Specific Debugging Tips

### Pipecat Pipelines
- **Common frame types**: `TranscriptionFrame`, `InterimTranscriptionFrame`, `TTSTextFrame`, `LLMTextFrame`, `AudioRawFrame`
- **Common issues**: Wrong pipeline order, missing frame forwarding in processors, broken context aggregation, VAD misconfiguration
- **Useful log points**: After `transport.input()`, before/after each processor, in observer `on_push_frame`, event handlers
- Use `pipecat-expert` agent for pipeline architecture questions

### FastAPI Services
- **Common issues**: Dependency injection failures, middleware ordering, async/sync mixing, background task errors
- **Useful log points**: Before/after dependencies, in middleware `dispatch`, exception handlers
- Use `fastapi-specialist` agent for endpoint/DI questions

### General Async Python
- **Common issues**: Uncaught exceptions in tasks, event loop blocking, race conditions, resource leaks
- **Useful log points**: `asyncio.Task` creation/completion, lock acquisition, resource open/close
- Use `python-debugger` agent for deep async debugging

---

## Error Recovery

### Service Won't Start
```bash
# Check for syntax errors
python -m py_compile <entry_point.py>
# Check imports
python -c "import <module>"
# Check environment
env | grep -i relevant_var
```

### Port Still in Use
```bash
lsof -i :<PORT>          # See what's using the port
kill -9 <PID>            # Kill specific process
```

### Git Reset Issues
```bash
git status               # Check state
git stash                # Stash if needed
git checkout -- <files>  # Reset specific files only
```

---

## Success Criteria

The RCA session is complete when:
- Root cause has been identified and confirmed with evidence
- Fix has been implemented and verified by user
- Changes have been committed (with user approval)
- All `[RCA]` instrumentation has been removed

**Begin by identifying the service and understanding the issue.**
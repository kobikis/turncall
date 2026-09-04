---
name: pr-review
description: "Reviews a GitHub pull request diff and produces a structured report covering correctness, security, Python best practices, and test coverage. Use when asked to review a PR, check a pull request, analyze a diff, or validate code before merging."
allowed-tools: Bash, Read, Grep, Glob
---

# PR Review

> Fetch a PR diff via `gh`, analyze it across 5 dimensions, and output a structured review report with inline findings.

## When to Activate

- User says "review PR", "review this pull request", "/pr-review", "check PR #123"
- User pastes a GitHub PR URL and asks for feedback
- After creating a PR with `/pr` and wanting a self-review before requesting teammates

---

## Workflow

### Step 1 — Fetch PR Diff

```bash
# By PR number (current repo)
gh pr diff <number>

# By URL
gh pr diff <url>

# Get PR metadata
gh pr view <number> --json title,body,additions,deletions,changedFiles,baseRefName,headRefName
```

- Output: raw diff + PR title/description/stats

### Step 2 — Identify Changed Files

Categorize each file:
- `src/` or app code → logic review
- `tests/` → test quality review
- `*.py` → Python-specific checks
- Config files (`*.toml`, `*.yml`, `*.env*`) → secrets + config review
- Migrations (`migrations/`) → database safety review

### Step 3 — Analyze Across 5 Dimensions

Run through each dimension for every changed file:

#### A. Correctness
- Logic errors, off-by-one, null/None handling
- Edge cases not covered
- Incorrect assumptions about input types

#### B. Security (CRITICAL — flag immediately)
- Hardcoded secrets, API keys, passwords
- SQL injection (string formatting into queries)
- Unsanitized user input passed to shell/eval/exec
- Insecure deserialization (pickle, yaml.load)
- Path traversal vulnerabilities

#### C. Python Best Practices
- Mutable default arguments (`def f(x=[])`)
- Bare `except:` clauses
- Missing type hints on public functions
- Mutation of function arguments
- Functions >50 lines (suggest refactor)
- Files >800 lines (suggest split)
- Magic numbers / hardcoded values

#### D. Test Coverage
- Are new functions covered by tests?
- Are edge cases tested?
- Are tests using `pytest.raises` for exception paths?
- Are external calls mocked?
- Does coverage appear to meet 80%+ threshold?

#### E. Code Quality
- Naming clarity (functions, variables, classes)
- Duplication (same logic appears 2+ times)
- Deep nesting (>4 levels)
- Commented-out code left in

### Step 4 — Produce Structured Report

Output the report in the format below.

---

## Decision Logic

**IF** no PR number or URL is provided:
→ Ask: "Which PR should I review? Provide a number or GitHub URL."

**IF** `gh` is not authenticated:
→ Show: `gh auth login` instructions and stop.

**IF** PR has >50 changed files:
→ Focus on files in `src/`, `app/`, or core logic. Skip lock files, generated files, assets.

**IF** security issue found (severity: CRITICAL):
→ Flag at top of report with `🚨 CRITICAL` before any other findings.

**IF** no tests changed but logic files changed:
→ Always flag as a finding under Test Coverage: "No tests added/modified for changed logic."

**DEFAULT**:
→ Run all 5 dimensions, produce full report.

---

## Output Format

```
## PR Review: <title> (#<number>)

**Branch**: `<head>` → `<base>`
**Changes**: +<additions> / -<deletions> across <N> files

---

### 🚨 Critical Issues
(Security or correctness blockers — must fix before merge)

- `path/to/file.py:42` — **Hardcoded API key** `SECRET_KEY = "abc123"`. Move to env var.

---

### ⚠️ Warnings
(Should fix — code quality, missing tests, non-critical bugs)

- `path/to/file.py:17` — **Mutable default argument** `def process(items=[])`. Use `None` and assign inside.
- `path/to/service.py` — **No tests added** for new `PaymentService.charge()` method.

---

### 💡 Suggestions
(Nice to have — style, naming, minor improvements)

- `path/to/utils.py:88` — Function `do_stuff()` is 73 lines. Consider splitting.
- `path/to/models.py:12` — Magic number `86400`. Extract as `SECONDS_IN_DAY = 86400`.

---

### ✅ Looks Good
- Test coverage added for happy path and edge cases
- Type hints present on all public functions
- No secrets detected

---

### Summary
| Dimension | Status |
|-----------|--------|
| Correctness | ✅ / ⚠️ / 🚨 |
| Security | ✅ / ⚠️ / 🚨 |
| Python Best Practices | ✅ / ⚠️ / 🚨 |
| Test Coverage | ✅ / ⚠️ / 🚨 |
| Code Quality | ✅ / ⚠️ / 🚨 |

**Verdict**: APPROVE / REQUEST CHANGES / NEEDS DISCUSSION
```

---

## Examples

**Input**: `/pr-review 42`
**Output**:
```
## PR Review: Add user authentication (#42)

**Branch**: `feat/auth` → `main`
**Changes**: +312 / -18 across 8 files

### 🚨 Critical Issues
- `app/auth.py:56` — SQL query built with string formatting. Use parameterized queries.

### ⚠️ Warnings
- `app/auth.py:23` — No test for invalid token path.
- `tests/test_auth.py` — `requests.post` not mocked; test hits real network.

### ✅ Looks Good
- Password hashed with bcrypt
- JWT expiry set correctly

**Verdict**: REQUEST CHANGES
```

---

**Input**: `/pr-review https://github.com/org/repo/pull/7`
**Output**: Full structured report for the linked PR.

---

## Edge Cases

| Scenario | Response |
|----------|----------|
| No PR number given | Ask for PR number or URL |
| `gh` not installed | Show install instructions: `brew install gh` |
| Draft PR | Note it's a draft; still review if asked |
| Only docs changed | Skip security/logic checks; focus on clarity |
| Binary or lock files in diff | Skip those files silently |
| Merge conflicts in diff | Flag as blocker in Critical Issues |
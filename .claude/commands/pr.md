---
description: Create a GitHub pull request. Analyzes all commits since branching from base, writes a comprehensive PR title and summary, then opens the PR with gh CLI.
---

# Create Pull Request

Creates a well-structured GitHub pull request for the current branch.

## Steps

1. **Verify prerequisites**
   - Check `gh auth status` — abort with clear message if not authenticated
   - Confirm current branch is not `main` or `master`

2. **Gather branch context**
   - Run `git status` to check for uncommitted changes (warn if any)
   - Run `git log main...HEAD --oneline` (fall back to `master` if `main` not found) to see all commits
   - Run `git diff main...HEAD --stat` to see changed files
   - Detect base branch automatically (`main` → `master` → `develop` fallback)

3. **Analyze all changes**
   - Review ALL commits in the branch (not just the latest)
   - Identify the type of change: feat / fix / refactor / docs / test / chore
   - Summarize what changed and why

4. **Push branch if needed**
   - Check if remote tracking branch exists
   - If not, push with `git push -u origin HEAD`

5. **Create the PR** using `gh pr create` with:
   - Short title (under 70 chars) following conventional commits style
   - Structured body (see template below)

## PR Body Template

```
## Summary
- <bullet: what was changed>
- <bullet: why / motivation>
- <bullet: any notable decisions>

## Changes
- <file or area>: <what changed>

## Test plan
- [ ] <specific thing to verify>
- [ ] <another thing to test>
- [ ] All existing tests pass

## Notes
<optional: breaking changes, deployment steps, follow-up issues>
```

## Example Usage

```
/pr
/pr "fix: resolve race condition in auth middleware"
```

If an argument is provided, use it as the PR title (skip title generation).

## Rules

- NEVER force-push or modify existing commits
- NEVER push directly to `main` or `master`
- If `gh` is not installed or authenticated, show clear error and stop
- If no commits differ from base branch, abort with message
- Always confirm the PR URL was created successfully at the end

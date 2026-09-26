# Git Workflow

## Commit Message Format

```
<type>: <description>

<optional body>
```

Types: feat, fix, refactor, docs, test, chore, perf, ci

Note: Attribution disabled globally via ~/.claude/settings.json.

## Sign off every commit (CI enforces this)

Use `git commit -s`. CI's `dco` job rejects any commit on the branch without a
`Signed-off-by:` trailer, so one unsigned commit fails the whole pull request —
including commits you did not write but are carrying on your branch.

```bash
git commit -s -m "fix: ..."          # new commit
git commit --amend -s                 # the last one
git rebase --signoff <base>           # every commit on the branch
```

The last form needs a force-push. Prefer `--force-with-lease`.

`Signed-off-by:` is the DCO certification and is separate from any
`Co-Authored-By:` trailer — adding the latter does not satisfy the former. See
CONTRIBUTING.md, "Developer Certificate of Origin".

Merging is the other half, and the gate does not cover it. A squash merge whose
body you supply by hand (`gh pr merge --squash --body-file ...`) replaces the
commit messages and drops their trailers — and the `dco` job only reads commits
in a pull request's range, so an unsigned squash commit reaches `main`
unchallenged. Keep the trailer in the body you pass, or let `gh` default to the
commit messages. Undoing it means rewriting published history, so it is worth
getting right on the first try.

## Pull Request Workflow

When creating PRs:
1. Analyze full commit history (not just latest commit)
2. Use `git diff [base-branch]...HEAD` to see all changes
3. Draft comprehensive PR summary
4. Include test plan with TODOs
5. Push with `-u` flag if new branch

## Feature Implementation Workflow

1. **Plan First**
   - Use **planner** agent to create implementation plan
   - Identify dependencies and risks
   - Break down into phases

2. **TDD Approach**
   - Use **tdd-guide** agent
   - Write tests first (RED)
   - Implement to pass tests (GREEN)
   - Refactor (IMPROVE)
   - Aim high on coverage (not gated in CI — see CONTRIBUTING.md)

3. **Code Review**
   - Use **code-reviewer** agent immediately after writing code
   - Address CRITICAL and HIGH issues
   - Fix MEDIUM issues when possible

4. **Commit & Push**
   - Detailed commit messages
   - Follow conventional commits format

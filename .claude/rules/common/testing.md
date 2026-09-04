# Testing Requirements

> These are working conventions for this project's AI tooling, not a
> contributor contract. For what CI actually enforces on a pull request, see
> [CONTRIBUTING.md](../../../CONTRIBUTING.md) — that document is authoritative
> and deliberately honest about the gaps.

## Coverage

**Aspiration: 80%. Not enforced.** There is no coverage gate in CI, and the
number is not currently measured on a per-PR basis. Aim high on new code;
don't claim a threshold the pipeline doesn't check.

Test types, in rough order of how much they earn their keep here:
1. **Unit Tests** - Individual functions, utilities, components
2. **Integration Tests** - API endpoints, database operations
3. **E2E Tests** - Critical user flows

## Test-Driven Development

Preferred workflow for new features and bug fixes:
1. Write test first (RED)
2. Run test - it should FAIL
3. Write minimal implementation (GREEN)
4. Run test - it should PASS
5. Refactor (IMPROVE)

## Troubleshooting Test Failures

1. Use **tdd-guide** agent
2. Check test isolation
3. Verify mocks are correct
4. Fix implementation, not tests (unless tests are wrong)

## Agent Support

- **tdd-guide** - Use PROACTIVELY for new features, enforces write-tests-first

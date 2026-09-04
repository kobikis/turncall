---
description: Enforce test-driven development workflow. Scaffold interfaces, generate tests FIRST, then implement minimal code to pass. Ensure 80%+ coverage.
---

# TDD Command

This command invokes the **tdd-guide** agent to enforce test-driven development methodology using **Python + pytest**.

## What This Command Does

1. **Scaffold Interfaces** - Define types/dataclasses/Pydantic models first
2. **Generate Tests First** - Write failing pytest tests (RED)
3. **Implement Minimal Code** - Write just enough to pass (GREEN)
4. **Refactor** - Improve code while keeping tests green (REFACTOR)
5. **Verify Coverage** - Ensure 80%+ test coverage via `pytest --cov`

## When to Use

Use `/tdd` when:
- Implementing new features
- Adding new functions/classes
- Fixing bugs (write test that reproduces bug first)
- Refactoring existing code
- Building critical business logic

## How It Works

The tdd-guide agent will:

1. **Define interfaces** using dataclasses, Pydantic models, or type hints
2. **Write tests that will FAIL** (because code doesn't exist yet)
3. **Run tests** and verify they fail for the right reason
4. **Write minimal implementation** to make tests pass
5. **Run tests** and verify they pass
6. **Refactor** code while keeping tests green
7. **Check coverage** and add more tests if below 80%

## TDD Cycle

```
RED → GREEN → REFACTOR → REPEAT

RED:      Write a failing pytest test
GREEN:    Write minimal code to pass
REFACTOR: Improve code, keep tests passing
REPEAT:   Next feature/scenario
```

## Example Usage

```
User: /tdd I need a function to calculate market liquidity score

Agent (tdd-guide):
# TDD Session: Market Liquidity Score Calculator

## Step 1: Define Interface (SCAFFOLD)

```python
# src/liquidity.py
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class MarketData:
    total_volume: float
    bid_ask_spread: float
    active_traders: int
    last_trade_time: datetime


def calculate_liquidity_score(market: MarketData) -> float:
    raise NotImplementedError
```

## Step 2: Write Failing Tests (RED)

```python
# tests/test_liquidity.py
from datetime import datetime, timedelta
import pytest
from src.liquidity import MarketData, calculate_liquidity_score


def liquid_market() -> MarketData:
    return MarketData(
        total_volume=100_000,
        bid_ask_spread=0.01,
        active_traders=500,
        last_trade_time=datetime.utcnow(),
    )


def illiquid_market() -> MarketData:
    return MarketData(
        total_volume=100,
        bid_ask_spread=0.5,
        active_traders=2,
        last_trade_time=datetime.utcnow() - timedelta(days=1),
    )


def test_high_score_for_liquid_market():
    score = calculate_liquidity_score(liquid_market())
    assert 80 < score <= 100


def test_low_score_for_illiquid_market():
    score = calculate_liquidity_score(illiquid_market())
    assert 0 <= score < 30


def test_zero_volume_returns_zero():
    market = MarketData(
        total_volume=0,
        bid_ask_spread=0.0,
        active_traders=0,
        last_trade_time=datetime.utcnow(),
    )
    assert calculate_liquidity_score(market) == 0.0
```

## Step 3: Run Tests – Verify FAIL

```bash
pytest tests/test_liquidity.py -v

FAILED tests/test_liquidity.py::test_high_score_for_liquid_market
  NotImplementedError
```

✅ Tests fail as expected. Ready to implement.

## Step 4: Implement Minimal Code (GREEN)

```python
# src/liquidity.py
from dataclasses import dataclass
from datetime import datetime, timezone

WEIGHTS = {"volume": 0.4, "spread": 0.3, "traders": 0.2, "recency": 0.1}
SCALE = {"volume": 1_000, "spread": 1_000, "traders": 10, "recency_penalty": 10}


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def calculate_liquidity_score(market: MarketData) -> float:
    if market.total_volume == 0:
        return 0.0

    volume_score  = _clamp(market.total_volume / SCALE["volume"])
    spread_score  = _clamp(100 - market.bid_ask_spread * SCALE["spread"])
    trader_score  = _clamp(market.active_traders / SCALE["traders"])

    now = datetime.now(tz=timezone.utc)
    last = market.last_trade_time.replace(tzinfo=timezone.utc) if market.last_trade_time.tzinfo is None else market.last_trade_time
    hours_ago = (now - last).total_seconds() / 3600
    recency_score = _clamp(100 - hours_ago * SCALE["recency_penalty"])

    return _clamp(
        volume_score  * WEIGHTS["volume"] +
        spread_score  * WEIGHTS["spread"] +
        trader_score  * WEIGHTS["traders"] +
        recency_score * WEIGHTS["recency"]
    )
```

## Step 5: Run Tests – Verify PASS

```bash
pytest tests/test_liquidity.py -v

PASSED tests/test_liquidity.py::test_high_score_for_liquid_market
PASSED tests/test_liquidity.py::test_low_score_for_illiquid_market
PASSED tests/test_liquidity.py::test_zero_volume_returns_zero

3 passed in 0.12s
```

✅ All tests passing!

## Step 6: Check Coverage

```bash
pytest tests/test_liquidity.py --cov=src.liquidity --cov-report=term-missing

Name               Stmts   Miss  Cover
--------------------------------------
src/liquidity.py      18      0   100%
```

✅ TDD session complete!
```

## TDD Best Practices

**DO:**
- ✅ Write the test FIRST, before any implementation
- ✅ Run tests and verify they FAIL before implementing
- ✅ Write minimal code to make tests pass
- ✅ Refactor only after tests are green
- ✅ Use `pytest` fixtures and `@pytest.mark.parametrize` for edge cases
- ✅ Aim for 80%+ coverage (100% for critical code)

**DON'T:**
- ❌ Write implementation before tests
- ❌ Skip running tests after each change
- ❌ Write too much code at once
- ❌ Ignore failing tests
- ❌ Test implementation details (test behavior)
- ❌ Mock everything (prefer integration tests)

## Test Types to Include

**Unit Tests** (Function-level):
- Happy path scenarios
- Edge cases (empty, None, max values)
- Error conditions and exceptions
- Boundary values
- Use `@pytest.mark.parametrize` for table-driven tests

**Integration Tests** (Component-level):
- FastAPI endpoints via `httpx.AsyncClient`
- Database operations via SQLAlchemy + test fixtures
- External service calls with `respx` or `pytest-mock`

**E2E Tests** (use `/e2e` command):
- Critical user flows
- Multi-step processes
- Full stack integration

## Coverage Requirements

- **80% minimum** for all code
- **100% required** for:
  - Financial calculations
  - Authentication logic
  - Security-critical code
  - Core business logic

Run coverage:
```bash
pytest --cov=src --cov-report=term-missing --cov-fail-under=80
```

## Important Notes

**MANDATORY**: Tests must be written BEFORE implementation. The TDD cycle is:

1. **RED** - Write failing test
2. **GREEN** - Implement to pass
3. **REFACTOR** - Improve code

Never skip the RED phase. Never write code before tests.

## Integration with Other Commands

- Use `/plan` first to understand what to build
- Use `/tdd` to implement with tests
- Use `/build-fix` if build errors occur
- Use `/code-review` to review implementation
- Use `/test-coverage` to verify coverage

## Related Agents

This command invokes the `tdd-guide` agent located at:
`~/.claude/agents/tdd-guide.md`

And can reference the `tdd-workflow` skill at:
`~/.claude/skills/tdd-workflow/`

---
name: tdd-workflow
description: Use this skill when writing new features, fixing bugs, or refactoring code. Enforces test-driven development with 80%+ coverage including unit, integration, and E2E tests.
---

# Test-Driven Development Workflow (Python)

This skill ensures all Python code follows TDD principles with comprehensive test coverage using **pytest**.

## When to Activate

- Writing new features or functionality
- Fixing bugs or issues
- Refactoring existing code
- Adding FastAPI endpoints
- Creating new services or utilities

## Core Principles

### 1. Tests BEFORE Code
ALWAYS write tests first, then implement code to make tests pass.

### 2. Coverage Requirements
- Minimum 80% coverage (unit + integration + E2E)
- All edge cases covered
- Error scenarios tested
- Boundary conditions verified

### 3. Test Types

#### Unit Tests
- Individual functions and utilities
- Pure functions
- Helpers, validators, and domain logic

#### Integration Tests
- FastAPI endpoints via `httpx.AsyncClient`
- Database operations via SQLAlchemy + test fixtures
- External service calls with `pytest-mock` or `respx`

#### E2E Tests (pytest + httpx)
- Critical API flows
- Complete multi-step workflows
- Full-stack integration scenarios

## TDD Workflow Steps

### Step 1: Write User Journeys
```
As a [role], I want to [action], so that [benefit]

Example:
As a user, I want to search for markets semantically,
so that I can find relevant markets even without exact keywords.
```

### Step 2: Generate Test Cases
For each user journey, create comprehensive test cases:

```python
# tests/test_search.py
import pytest

class TestSemanticSearch:
    def test_returns_relevant_markets_for_query(self): ...
    def test_handles_empty_query_gracefully(self): ...
    def test_falls_back_when_redis_unavailable(self): ...
    def test_sorts_results_by_similarity_score(self): ...
```

### Step 3: Run Tests (They Should Fail)
```bash
pytest tests/test_search.py -v
# Tests should FAIL — implementation doesn't exist yet
```

### Step 4: Implement Code
Write minimal code to make tests pass:

```python
# src/search.py
async def search_markets(query: str) -> list[dict]:
    # Minimal implementation guided by tests
    ...
```

### Step 5: Run Tests Again
```bash
pytest tests/test_search.py -v
# All tests should now PASS
```

### Step 6: Refactor
Improve code quality while keeping tests green:
- Remove duplication
- Improve naming
- Optimize performance
- Enhance readability

### Step 7: Verify Coverage
```bash
pytest --cov=src --cov-report=term-missing --cov-fail-under=80
```

## Testing Patterns

### Unit Test Pattern (pytest)
```python
# tests/unit/test_validators.py
import pytest
from src.validators import validate_market_name


@pytest.mark.parametrize("name,expected", [
    ("Valid Name", True),
    ("", False),
    ("a" * 201, False),
    ("Valid-Name_123", True),
])
def test_validate_market_name(name: str, expected: bool):
    assert validate_market_name(name) == expected


def test_validate_market_name_strips_whitespace():
    assert validate_market_name("  Valid  ") is True


def test_validate_market_name_raises_on_none():
    with pytest.raises(TypeError):
        validate_market_name(None)  # type: ignore
```

### FastAPI Integration Test Pattern
```python
# tests/integration/test_markets_api.py
import pytest
from httpx import AsyncClient, ASGITransport
from src.main import app


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_list_markets_returns_200(client: AsyncClient):
    response = await client.get("/api/markets")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert isinstance(body["data"], list)


@pytest.mark.anyio
async def test_list_markets_rejects_invalid_limit(client: AsyncClient):
    response = await client.get("/api/markets?limit=invalid")
    assert response.status_code == 422


@pytest.mark.anyio
async def test_create_market_returns_201(client: AsyncClient, auth_headers: dict):
    payload = {"name": "Test Market", "description": "Desc", "end_date": "2026-12-31"}
    response = await client.post("/api/markets", json=payload, headers=auth_headers)
    assert response.status_code == 201
    assert response.json()["data"]["name"] == "Test Market"
```

### E2E Test Pattern (pytest + httpx)
```python
# tests/e2e/test_market_workflow.py
import pytest
from httpx import AsyncClient, ASGITransport
from src.main import app


@pytest.mark.anyio
async def test_full_market_lifecycle(client: AsyncClient, auth_headers: dict):
    # Create
    create_resp = await client.post(
        "/api/markets",
        json={"name": "E2E Market", "description": "test", "end_date": "2026-12-31"},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201
    market_id = create_resp.json()["data"]["id"]

    # Read
    get_resp = await client.get(f"/api/markets/{market_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["data"]["name"] == "E2E Market"

    # Delete
    del_resp = await client.delete(f"/api/markets/{market_id}", headers=auth_headers)
    assert del_resp.status_code == 204

    # Confirm gone
    gone_resp = await client.get(f"/api/markets/{market_id}")
    assert gone_resp.status_code == 404
```

## Test File Organization

```
src/
├── markets/
│   ├── router.py
│   ├── service.py
│   └── models.py
tests/
├── conftest.py              # shared fixtures (db, client, auth_headers)
├── unit/
│   ├── test_validators.py
│   ├── test_service.py
│   └── test_utils.py
├── integration/
│   ├── test_markets_api.py
│   └── test_db_operations.py
└── e2e/
    └── test_market_workflow.py
```

## Mocking External Services

### SQLAlchemy / DB Mock
```python
from unittest.mock import AsyncMock, patch

async def test_service_handles_db_error():
    with patch("src.markets.service.get_db") as mock_db:
        mock_db.return_value.__aenter__.return_value.execute = AsyncMock(
            side_effect=Exception("DB error")
        )
        with pytest.raises(ServiceError):
            await get_market_by_id(1)
```

### HTTP Client Mock (respx)
```python
import respx
import httpx

@respx.mock
async def test_external_api_call():
    respx.get("https://api.external.com/data").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )
    result = await fetch_external_data()
    assert result == {"result": "ok"}
```

### pytest-mock for Functions
```python
def test_search_falls_back_on_redis_failure(mocker):
    mocker.patch("src.search.redis_client.search", side_effect=ConnectionError)
    results = search_markets("election")
    assert isinstance(results, list)  # fallback returns empty list, not exception
```

## Coverage Configuration (pyproject.toml)

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.coverage.run]
source = ["src"]
omit = ["src/main.py", "*/migrations/*"]

[tool.coverage.report]
fail_under = 80
show_missing = true
```

## Common Mistakes to Avoid

### ❌ WRONG: Testing Implementation Details
```python
# Don't assert on internal state
assert service._cache == {}
```

### ✅ CORRECT: Test Observable Behavior
```python
# Assert on outputs and side effects
result = service.get_market(1)
assert result.name == "Test Market"
```

### ❌ WRONG: Tests That Depend on Each Other
```python
def test_update_market():
    # Relies on test_create_market having run first
    update_market(GLOBAL_ID, name="New")
```

### ✅ CORRECT: Isolated Tests with Fixtures
```python
@pytest.fixture
def market(db_session):
    return MarketFactory.create(session=db_session)

def test_update_market(market):
    updated = update_market(market.id, name="New")
    assert updated.name == "New"
```

## Continuous Testing

### Watch Mode
```bash
ptw tests/          # pytest-watch: re-runs on file change
```

### Pre-Commit Hook
```bash
pytest && ruff check src/
```

### CI/CD Integration (GitHub Actions)
```yaml
- name: Run Tests
  run: pytest --cov=src --cov-report=xml --cov-fail-under=80
- name: Upload Coverage
  uses: codecov/codecov-action@v4
```

## Best Practices

1. **Write Tests First** — Always TDD
2. **One Assert Per Test** — Focus on single behavior
3. **Descriptive Test Names** — `test_<what>_<condition>_<expected>`
4. **Arrange-Act-Assert** — Clear test structure
5. **Use Fixtures** — `conftest.py` for shared setup/teardown
6. **Use `@pytest.mark.parametrize`** — Table-driven edge cases
7. **Test Error Paths** — `pytest.raises`, not just happy paths
8. **Keep Unit Tests Fast** — < 50ms each; mock I/O
9. **Clean Up After Tests** — Use `db.rollback()` or transaction fixtures
10. **Review Coverage Reports** — `--cov-report=term-missing` shows uncovered lines

## Success Metrics

- 80%+ code coverage achieved
- All tests passing (green)
- No skipped or disabled tests
- Fast unit test suite (< 30s)
- Integration tests cover all endpoints
- E2E tests cover critical user flows

---

**Remember**: Tests are not optional. They are the safety net that enables confident refactoring, rapid development, and production reliability.

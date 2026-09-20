"""The test-suite stub is gone, and stays gone.

`test_suites` / `test_runs` accepted rows through four endpoints for the life
of the project and nothing ever executed them — the create-run docstring
promised a background worker that was never written. The evals feature (#68)
takes the namespace, so the stub is deleted rather than migrated.

These assertions are cheap insurance against it being reintroduced by a
copy-paste or a revert, which is how dead endpoints usually come back.
"""

import pytest

pytestmark = pytest.mark.unit


def test_the_stub_endpoints_are_not_routed(app) -> None:
    for path in ("/v1/test-suites", "/v1/test-runs"):
        assert app.get(path).status_code == 404
        assert app.post(path, json={}).status_code == 404


def test_the_row_models_are_gone() -> None:
    from turncall.storage import models

    assert not hasattr(models, "TestSuiteRow")
    assert not hasattr(models, "TestRunRow")
    tables = models.Base.metadata.tables
    assert "test_suites" not in tables
    assert "test_runs" not in tables


def test_the_openapi_document_no_longer_advertises_testing(app) -> None:
    spec = app.get("/openapi.json").json()
    tags = {
        t
        for p in spec["paths"].values()
        for op in p.values()
        for t in op.get("tags", [])
    }
    assert "testing" not in tags

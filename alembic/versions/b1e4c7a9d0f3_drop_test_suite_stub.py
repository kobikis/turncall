"""drop the dead test-suite stub

`test_suites` and `test_runs` shipped in the initial schema and were never
executed. Four endpoints accepted rows; the create-run docstring promised a
background worker that was never written, and nothing outside the router and
the models ever referenced either table. The evals feature (#68) replaces the
namespace with `eval_scenarios` / `eval_runs`.

One-way. The downgrade raises rather than recreating tables whose rows were
never meaningful: restoring empty tables would claim a rollback that does not
exist, and any row that did get written described a run that never happened.

Revision ID: b1e4c7a9d0f3
Revises: a9c1d3e5f7b2
Create Date: 2026-09-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b1e4c7a9d0f3"
down_revision: str | None = "a9c1d3e5f7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # test_runs first: it carries the FK to test_suites.
    op.drop_index("ix_test_runs_suite_id", table_name="test_runs")
    op.drop_table("test_runs")
    op.drop_index("ix_test_suites_project_id", table_name="test_suites")
    op.drop_table("test_suites")
    # The enum is only reachable from the dropped column.
    op.execute("DROP TYPE IF EXISTS test_run_status")


def downgrade() -> None:
    raise NotImplementedError(
        "Dropping the test-suite stub is one-way: the tables were never "
        "executed, so there is no state to restore."
    )

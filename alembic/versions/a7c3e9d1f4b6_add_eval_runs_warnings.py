"""add eval_runs.warnings

A run carries things its author has to know that are not verdicts (#96): a tool
mock that can never fire because the agent has no such tool, or because it is an
MCP tool an eval never advertises. Those were `logger.warning` calls in the
worker, and the person who wrote the scenario reads the run — the API, the
Console, the CLI — not a container's log. From where they stand a typo'd mock, a
mock on an MCP tool and a tool the agent simply chose not to call all looked the
same: a green run with an inert mock.

Empty list for every run that predates this, which is the honest answer: nothing
was recorded, not "nothing was wrong".

Revision ID: a7c3e9d1f4b6
Revises: d3b8c1f4a205
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7c3e9d1f4b6"
down_revision: str | None = "d3b8c1f4a205"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "eval_runs",
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("eval_runs", "warnings")

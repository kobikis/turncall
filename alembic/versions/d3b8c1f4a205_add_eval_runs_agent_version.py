"""add eval_runs.agent_version

A run targeting an agent by name resolves to whichever version is published at
the time (#74), and the run has to say which one that was. `agent_id` names the
row and an agent row is an immutable version — but the column carries no
foreign key, so the row can be deleted and take the answer with it. A result
nobody can interpret later is the thing ADR-0017's snapshots exist to prevent.

NULL for an inline target (there is no version) and for runs that predate this.

Revision ID: d3b8c1f4a205
Revises: c2f5a8b3e701
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3b8c1f4a205"
down_revision: str | None = "c2f5a8b3e701"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "eval_runs", sa.Column("agent_version", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("eval_runs", "agent_version")

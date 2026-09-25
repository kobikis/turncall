"""add eval_scenarios.judge / .simulator

The judge decides `eval:` assertions and every simulation verdict, and the
persona plays the caller. Both were configurable only by hand-writing pipecat's
own block inside `definition`, and only as a local Ollama — anything else needs
pipecat's `factory`, a dotted path it imports, which cannot be accepted from an
API body (#118).

TurnCall columns rather than keys inside `definition`, for the reason
`tool_mocks` already is one: mixing ownership would mean a future pipecat
migration had to preserve fields inside a mapping it does not own.

NULL for every scenario that predates this, which keeps pipecat's default.

Revision ID: c5f1a7d3e9b2
Revises: a7c3e9d1f4b6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c5f1a7d3e9b2"
down_revision: str | None = "a7c3e9d1f4b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column in ("judge", "simulator"):
        op.add_column(
            "eval_scenarios",
            sa.Column(column, postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        )


def downgrade() -> None:
    for column in ("judge", "simulator"):
        op.drop_column("eval_scenarios", column)

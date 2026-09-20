"""eval_scenarios and eval_runs

Automated behavioural testing for agents (#68). A scenario is one saved test,
kind-discriminated; a run is one scenario x target x modality over N
iterations.

`definition` is opaque JSONB on purpose -- it is pipecat's own scenario
mapping, which moves between majors, so `schema_version` records which schema
it targets rather than Alembic tracking pipecat's schema forever.

Revision ID: c2f5a8b3e701
Revises: b1e4c7a9d0f3
Create Date: 2026-09-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c2f5a8b3e701"
down_revision: str | None = "b1e4c7a9d0f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# create_type=False: the enums are created once, explicitly, below. Letting two
# tables that share `eval_kind` each try to create it is a duplicate-type error.
EVAL_KIND = postgresql.ENUM("script", "simulation", name="eval_kind", create_type=False)
EVAL_MODALITY = postgresql.ENUM("text", "audio", name="eval_modality", create_type=False)
EVAL_RUN_STATUS = postgresql.ENUM(
    "queued",
    "running",
    "passed",
    "failed",
    "errored",
    "cancelled",
    name="eval_run_status",
    create_type=False,
)
EVAL_TOOL_POLICY = postgresql.ENUM(
    "mock_only", "live", name="eval_tool_policy", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in (EVAL_KIND, EVAL_MODALITY, EVAL_RUN_STATUS, EVAL_TOOL_POLICY):
        enum.create(bind, checkfirst=True)

    op.create_table(
        "eval_scenarios",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("kind", EVAL_KIND, nullable=False),
        sa.Column("definition", postgresql.JSONB(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column(
            "tool_mocks",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "tool_policy",
            EVAL_TOOL_POLICY,
            nullable=False,
            server_default="mock_only",
        ),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("default_target", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name", name="uq_eval_scenarios_project_name"),
    )
    op.create_index("ix_eval_scenarios_project", "eval_scenarios", ["project_id"])
    op.create_index(
        "ix_eval_scenarios_tags",
        "eval_scenarios",
        ["tags"],
        postgresql_using="gin",
    )

    op.create_table(
        "eval_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("scenario_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("scenario_name", sa.String(length=255), nullable=False),
        sa.Column("kind", EVAL_KIND, nullable=False),
        sa.Column("target", postgresql.JSONB(), nullable=False),
        sa.Column("resolved_config", postgresql.JSONB(), nullable=False),
        sa.Column("resolved_scenario", postgresql.JSONB(), nullable=False),
        sa.Column("harness_config", postgresql.JSONB(), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("modality", EVAL_MODALITY, nullable=False),
        sa.Column("iterations", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", EVAL_RUN_STATUS, nullable=False, server_default="queued"),
        sa.Column("passed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "results",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        # SET NULL, not CASCADE: deleting a scenario must not destroy the record
        # of what it once proved. scenario_name keeps the row readable.
        sa.ForeignKeyConstraint(
            ["scenario_id"], ["eval_scenarios.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_eval_runs_project_queued", "eval_runs", ["project_id", "queued_at"]
    )
    op.create_index("ix_eval_runs_batch", "eval_runs", ["batch_id"])
    op.create_index("ix_eval_runs_scenario", "eval_runs", ["scenario_id"])


def downgrade() -> None:
    op.drop_index("ix_eval_runs_scenario", table_name="eval_runs")
    op.drop_index("ix_eval_runs_batch", table_name="eval_runs")
    op.drop_index("ix_eval_runs_project_queued", table_name="eval_runs")
    op.drop_table("eval_runs")
    op.drop_index("ix_eval_scenarios_tags", table_name="eval_scenarios")
    op.drop_index("ix_eval_scenarios_project", table_name="eval_scenarios")
    op.drop_table("eval_scenarios")
    bind = op.get_bind()
    for enum in (EVAL_TOOL_POLICY, EVAL_RUN_STATUS, EVAL_MODALITY, EVAL_KIND):
        enum.drop(bind, checkfirst=True)

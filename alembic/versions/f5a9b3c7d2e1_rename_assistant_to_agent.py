"""rename assistant to agent

Revision ID: f5a9b3c7d2e1
Revises: b4c8d2e6f3a7
Create Date: 2026-04-14 10:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f5a9b3c7d2e1"
down_revision: str | None = "b4c8d2e6f3a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Rename enum type: assistant_state -> agent_state
    op.execute("ALTER TYPE assistant_state RENAME TO agent_state")

    # 2. Update routing_target_type enum value: 'assistant' -> 'agent'
    op.execute("ALTER TYPE routing_target_type RENAME VALUE 'assistant' TO 'agent'")

    # 3. Rename table: assistants -> agents
    op.rename_table("assistants", "agents")

    # 4. Rename columns
    op.alter_column("calls", "active_assistant_id", new_column_name="active_agent_id")
    op.alter_column("sms_sessions", "assistant_id", new_column_name="agent_id")
    op.alter_column("test_suites", "assistant_id", new_column_name="agent_id")

    # 5. Rename indexes on the agents table (formerly assistants)
    op.execute("ALTER INDEX ix_assistants_project_env RENAME TO ix_agents_project_env")
    op.execute(
        "ALTER INDEX ix_assistants_project_name_version "
        "RENAME TO ix_agents_project_name_version"
    )

    # 6. Update JSONB config_blob: rename handoff_to_assistant -> handoff_to_agent
    op.execute(
        """
        UPDATE agents
        SET config_blob = replace(config_blob::text, '"handoff_to_assistant"', '"handoff_to_agent"')::jsonb
        WHERE config_blob::text LIKE '%handoff_to_assistant%'
        """
    )

    # 7. Update JSONB config_blob: rename transfer_to_human -> transfer_call
    #    (in case any old configs still reference the pre-rename tool)
    op.execute(
        """
        UPDATE agents
        SET config_blob = replace(config_blob::text, '"transfer_to_human"', '"transfer_call"')::jsonb
        WHERE config_blob::text LIKE '%transfer_to_human%'
        """
    )


def downgrade() -> None:
    # Reverse all operations

    # 7. Revert JSONB tool name: transfer_call -> transfer_to_human
    op.execute(
        """
        UPDATE agents
        SET config_blob = replace(config_blob::text, '"transfer_call"', '"transfer_to_human"')::jsonb
        WHERE config_blob::text LIKE '%transfer_call%'
        """
    )

    # 6. Revert JSONB tool name: handoff_to_agent -> handoff_to_assistant
    op.execute(
        """
        UPDATE agents
        SET config_blob = replace(config_blob::text, '"handoff_to_agent"', '"handoff_to_assistant"')::jsonb
        WHERE config_blob::text LIKE '%handoff_to_agent%'
        """
    )

    # 5. Rename indexes back
    op.execute("ALTER INDEX ix_agents_project_env RENAME TO ix_assistants_project_env")
    op.execute(
        "ALTER INDEX ix_agents_project_name_version "
        "RENAME TO ix_assistants_project_name_version"
    )

    # 4. Rename columns back
    op.alter_column("calls", "active_agent_id", new_column_name="active_assistant_id")
    op.alter_column("sms_sessions", "agent_id", new_column_name="assistant_id")
    op.alter_column("test_suites", "agent_id", new_column_name="assistant_id")

    # 3. Rename table back
    op.rename_table("agents", "assistants")

    # 2. Revert routing_target_type enum value
    op.execute("ALTER TYPE routing_target_type RENAME VALUE 'agent' TO 'assistant'")

    # 1. Rename enum type back
    op.execute("ALTER TYPE agent_state RENAME TO assistant_state")

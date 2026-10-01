"""a phone number's own Twilio account

Account SID in the clear, Auth Token encrypted under CREDENTIAL_ENCRYPTION_KEY.
Null on both = the platform's TWILIO_* account, as before.

Revision ID: f4b8d2a6c1e9
Revises: e3a7c1f9b4d2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4b8d2a6c1e9"
down_revision: str | None = "e3a7c1f9b4d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "phone_numbers", sa.Column("twilio_account_sid", sa.String(64), nullable=True)
    )
    op.add_column(
        "phone_numbers",
        sa.Column("twilio_auth_token_ciphertext", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_phone_numbers_twilio_account_sid", "phone_numbers", ["twilio_account_sid"]
    )


def downgrade() -> None:
    op.drop_index("ix_phone_numbers_twilio_account_sid", table_name="phone_numbers")
    op.drop_column("phone_numbers", "twilio_auth_token_ciphertext")
    op.drop_column("phone_numbers", "twilio_account_sid")

"""s3 access keys

The access-key credential table. The secret is stored only sealed under
`MASTER_KEY` (`sealed_secret`), never in the clear. A unique index on the key
id serves the signing hot path; an index on the owner serves listing and
rotation.

Revision ID: d3b9f2c6a7e8
Revises: c2a8e1f4b5d6
Create Date: 2026-07-24 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3b9f2c6a7e8"
down_revision: str | None = "c2a8e1f4b5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "s3_access_keys",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=False),
        sa.Column("sealed_secret", sa.LargeBinary(), nullable=False),
        sa.Column("secret_master_key_id", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("owner_subject", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_s3_access_keys_key_id", "s3_access_keys", ["key_id"], unique=True)
    op.create_index("ix_s3_access_keys_owner", "s3_access_keys", ["owner_subject"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_s3_access_keys_owner", table_name="s3_access_keys")
    op.drop_index("uq_s3_access_keys_key_id", table_name="s3_access_keys")
    op.drop_table("s3_access_keys")

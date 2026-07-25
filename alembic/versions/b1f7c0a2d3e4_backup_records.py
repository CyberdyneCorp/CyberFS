"""backup records

Revision ID: b1f7c0a2d3e4
Revises: 079de84e4f44
Create Date: 2026-07-24 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1f7c0a2d3e4"
down_revision: str | None = "079de84e4f44"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backup_records",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("schema_revision", sa.String(length=64), nullable=False),
        sa.Column("dump_checksum", sa.String(length=64), nullable=True),
        sa.Column("object_count", sa.BigInteger(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("skew_missing_in_dump", sa.Integer(), nullable=False),
        sa.Column("skew_missing_in_manifest", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_backup_records_state_finished",
        "backup_records",
        ["state", "finished_at"],
        unique=False,
    )
    op.create_index("ix_backup_records_started", "backup_records", ["started_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_backup_records_started", table_name="backup_records")
    op.drop_index("ix_backup_records_state_finished", table_name="backup_records")
    op.drop_table("backup_records")

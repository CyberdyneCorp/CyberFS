"""audit protocol

Distinguish REST from S3 traffic on the audit record, and confirm the summary
query's index on `(actor_subject, occurred_at)` is present.

Revision ID: c2a8e1f4b5d6
Revises: b1f7c0a2d3e4
Create Date: 2026-07-24 13:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2a8e1f4b5d6"
down_revision: str | None = "b1f7c0a2d3e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_records",
        sa.Column(
            "protocol",
            sa.String(length=16),
            nullable=False,
            server_default="rest",
        ),
    )
    # The activity summary is answered from actor + time. The index already
    # ships with the initial schema; create it defensively so a database that
    # predates it is brought up to par without failing if it is already there.
    op.create_index(
        "ix_audit_actor",
        "audit_records",
        ["actor_subject", "occurred_at"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_column("audit_records", "protocol")

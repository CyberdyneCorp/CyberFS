"""grant pending state

Adds the `pending` flag to `grants`. A grant over a subtree larger than
`ASYNC_REWRAP_THRESHOLD_NODES` is created pending: its DEK rewrap is handed to
the background worker and the grant confers no access until every encrypted
descendant is rewrapped for the recipient. Existing rows default to active, so
every previously synchronous share stays usable. A partial index on the pending
rows drives the worker's scan without weighing on the active majority.

Revision ID: f5d1b6e2c9a0
Revises: e4c0a3d7b8f9
Create Date: 2026-07-24 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f5d1b6e2c9a0"
down_revision: str | None = "e4c0a3d7b8f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "grants",
        sa.Column(
            "pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_grants_pending",
        "grants",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("pending"),
    )


def downgrade() -> None:
    op.drop_index("ix_grants_pending", table_name="grants")
    op.drop_column("grants", "pending")

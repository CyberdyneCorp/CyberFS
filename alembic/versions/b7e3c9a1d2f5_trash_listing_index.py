"""owner-scoped trash index

Adds one partial index for the trash listing: `(owner_id, deleted_at)` where
`deleted_at IS NOT NULL`. Without it, presenting a user's trash means scanning
every trashed row in the deployment and discarding the ones belonging to someone
else -- `ix_nodes_deleted_at` cannot serve the query because it is deliberately
not owner-scoped, the retention sweep it exists for reading across all users.

Index-only, so it is safe to apply and to roll back at any time: no column, no
constraint, and no data is touched.

Revision ID: b7e3c9a1d2f5
Revises: a6e2c1d4f7b3
Create Date: 2026-07-27 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e3c9a1d2f5"
down_revision: str | None = "a6e2c1d4f7b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_nodes_owner_trash",
        "nodes",
        ["owner_id", "deleted_at"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_nodes_owner_trash",
        table_name="nodes",
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
    )

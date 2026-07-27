"""node tags and metadata

Adds `node_tags` and `node_metadata`, the searchable labels and key/value pairs
a caller may attach to any node. A row per tag and per pair rather than an array
or JSONB column: the per-node limits are then a plain count, and a filtered
search is an ordinary join on an indexed column instead of a containment
operator.

Both cascade on `node_id`, so delete and purge need no new cleanup -- the same
mechanism `file_versions`, `grants` and `public_links` already rely on.

Both tables start empty, every response field they feed is optional, and the
search parameters that read them are optional too, so an existing client sees no
change.

Revision ID: a6e2c1d4f7b3
Revises: f5d1b6e2c9a0
Create Date: 2026-07-27 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a6e2c1d4f7b3"
down_revision: str | None = "f5d1b6e2c9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "node_tags",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tag", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("node_id", "tag", name="uq_node_tags_node_tag"),
    )
    # Searching by tag looks the tag up first, then narrows to accessible nodes.
    op.create_index("ix_node_tags_tag", "node_tags", ["tag"], unique=False)

    op.create_table(
        "node_metadata",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.String(length=1024), nullable=False),
        sa.UniqueConstraint("node_id", "key", name="uq_node_metadata_node_key"),
    )
    # Serves both supported queries: key alone, and key together with value.
    op.create_index("ix_node_metadata_key_value", "node_metadata", ["key", "value"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_node_metadata_key_value", table_name="node_metadata")
    op.drop_table("node_metadata")
    op.drop_index("ix_node_tags_tag", table_name="node_tags")
    op.drop_table("node_tags")

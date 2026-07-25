"""multipart uploads

The in-flight multipart-upload tables. `multipart_uploads` records each upload's
target and creation time (indexed for the reaper's abandonment sweep);
`multipart_parts` records one staged part per row, keyed to its upload so
deleting the upload cascades its parts. A part number is unique within an upload
so a re-uploaded part replaces the earlier one.

Revision ID: e4c0a3d7b8f9
Revises: d3b9f2c6a7e8
Create Date: 2026-07-24 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4c0a3d7b8f9"
down_revision: str | None = "d3b9f2c6a7e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "multipart_uploads",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("upload_id", sa.String(length=64), nullable=False),
        sa.Column("initiator_subject", sa.String(length=255), nullable=False),
        sa.Column("target_owner_subject", sa.String(length=255), nullable=False),
        sa.Column("target_key", sa.String(length=2048), nullable=False),
        sa.Column("via_shared", sa.Boolean(), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_multipart_uploads_upload_id", "multipart_uploads", ["upload_id"], unique=True
    )
    op.create_index(
        "ix_multipart_uploads_created_at", "multipart_uploads", ["created_at"], unique=False
    )

    op.create_table(
        "multipart_parts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("upload_id", sa.String(length=64), nullable=False),
        sa.Column("part_number", sa.Integer(), nullable=False),
        sa.Column("etag", sa.String(length=64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["upload_id"], ["multipart_uploads.upload_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("upload_id", "part_number", name="uq_multipart_parts_upload_number"),
    )
    op.create_index("ix_multipart_parts_upload", "multipart_parts", ["upload_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_multipart_parts_upload", table_name="multipart_parts")
    op.drop_table("multipart_parts")
    op.drop_index("ix_multipart_uploads_created_at", table_name="multipart_uploads")
    op.drop_index("uq_multipart_uploads_upload_id", table_name="multipart_uploads")
    op.drop_table("multipart_uploads")

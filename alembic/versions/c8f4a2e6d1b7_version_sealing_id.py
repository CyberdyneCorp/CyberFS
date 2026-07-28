"""Record the version id each version's bytes were sealed under.

`seal` binds the version id into the AEAD as associated data, and `open`
authenticates against it. Copy and version-restore write a new row for bytes that
were sealed under a *different* id, so decryption authenticated against an id the
bytes were never sealed under and returned nothing. Making the sealing id an
explicit column is what lets copied content stay readable without being
re-encrypted.

The backfill is exact, not approximate. Every row that exists was written by the
in-place seal path and was therefore sealed under its own id. Rows produced by the
broken copy paths were sealed under some other id, are already unreadable, and
cannot be repaired here -- the source id was never recorded anywhere, so there is
nothing to recover it from. Unencrypted rows have no associated data at all, so
the value is inert for them.

Three statements rather than one because a `server_default` cannot reference
another column in Postgres: add nullable, backfill, then set NOT NULL.

Revision ID: c8f4a2e6d1b7
Revises: b7e3c9a1d2f5
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c8f4a2e6d1b7"
down_revision: str | None = "b7e3c9a1d2f5"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "file_versions",
        sa.Column("seal_version_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # Deliberately no foreign key: this routinely points at a version of another
    # node and must keep pointing there after that version is pruned by
    # VERSION_RETENTION_COUNT. A cascade would delete a healthy copy; a restrict
    # would block a legitimate prune.
    op.execute("UPDATE file_versions SET seal_version_id = id")
    op.alter_column("file_versions", "seal_version_id", nullable=False)


def downgrade() -> None:
    """Drop the column.

    Lossy in a way worth naming: afterwards the schema again implies that a
    version's bytes were sealed under its own id, so any copy made while the
    column existed becomes unreadable. That is precisely what the previous schema
    meant, so this restores the old behaviour rather than merely the old shape.
    """
    op.drop_column("file_versions", "seal_version_id")

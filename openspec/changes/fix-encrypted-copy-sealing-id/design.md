# Design

## The invariant being restored

Sealed bytes and the id they were sealed under are one unit. Any row that points
at an object must also state which id opens it. Before this change the second half
was inferred from the first, which held only for content sealed in place.

Stated as the rule the code now follows: **`seal_version_id` travels with the
bytes, `id` identifies the row.** For content sealed in place the two coincide,
which is why the bug stayed invisible for so long — the common case makes them
indistinguishable.

## Why a column and not a re-seal

Re-sealing is the obvious alternative and it is genuinely correct. It was
rejected on cost and on clarity.

On cost: a copy and a version restore are metadata operations today — one
`ObjectStore.copy`, which for MinIO is a server-side copy that never moves bytes
through CyberFS. Re-sealing makes both O(size): download, decrypt, re-encrypt,
upload. A 5 GB file restore becomes a 10 GB transfer and two AEAD passes, on a
request a user thinks of as "undo".

On clarity: re-sealing keeps the AAD implicit. The rule would remain "the AAD is
the row's id", enforced by remembering to re-seal at every site that ever creates
a row for existing bytes. That is the same latent trap, reset. A column makes the
binding explicit and makes the next such site fail loudly — a new copy path that
forgets to carry `seal_version_id` gets the column default, which is its own id,
and the existing tests catch it.

## The column

`seal_version_id UUID NOT NULL` on `file_versions`.

Not nullable with a "null means use id" reading: that is the implicit rule again,
one indirection further along, and every reader would have to remember the
fallback. A `NOT NULL` column that is usually equal to `id` costs 16 bytes and
removes the question.

No foreign key. `seal_version_id` frequently points at a version of a *different
node*, and it must survive that version being pruned by
`VERSION_RETENTION_COUNT` — the bytes stay readable regardless of whether the row
they were originally sealed under still exists. An FK with `ON DELETE CASCADE`
would delete the copy; `RESTRICT` would block a legitimate prune. So the column
is deliberately a bare identifier, and the migration says so.

## Backfill

`UPDATE file_versions SET seal_version_id = id` for every existing row.

This is exactly right rather than merely convenient. Every row created by the
in-place seal path was sealed under its own id. Rows created by copy or restore
were sealed under some *other* id and are already unreadable — the backfill does
not make them worse, and nothing can make them better without knowing which
source they came from, which was never recorded. Unencrypted rows have no AAD at
all, so the value is inert for them.

The migration adds the column with `server_default=sa.text("id")`... which
Postgres refuses, because a default cannot reference another column. So the column
is added nullable, backfilled with one `UPDATE`, then set `NOT NULL` — three
statements in one transaction, the standard shape for a non-null addition with no
constant default.

`downgrade` drops the column. That loses the distinction and returns the database
to a state where copies are unreadable, which is what the previous schema meant;
the migration notes it rather than pretending the downgrade is lossless.

## Where the value comes from

| Path | `seal_version_id` |
|---|---|
| First upload, and every content replacement | the new row's own `id` |
| `restore_version` | the **source** version's `seal_version_id` |
| `_copy_content` | the **source** version's `seal_version_id` |

The transitive case matters and falls out for free: copying a copy carries the
original's id, because the source row already holds it rather than holding its own
id. Chasing a chain at read time is never needed.

## What does not change

`seal` still takes the version id it is sealing under, and callers still pass the
id of the row they are creating — sealing in place is unchanged. Only `open`
changes, from `version.id` to `version.seal_version_id`.

The replay protection is intact and is worth restating, because this change moves
the thing that provides it. Two versions of one node share a DEK (see
`ensure_data_key`), so the AAD is the only thing separating their frames. After
this change a *copy* shares both the DEK and the AAD with its source — which is
correct, because a copy is the same content: the frames are interchangeable
because the plaintext is identical, so there is nothing to gain by moving one into
the other. Distinct content never shares an AAD, because distinct content is
always sealed in place under its own new id.

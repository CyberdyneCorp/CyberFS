"""S3-compatible surface: the pure heart of the S3 protocol.

The domain here owns the credential model -- an access key resolves to an
existing CyberdyneAuth subject and carries no identity of its own -- the pure
SigV4 signing algorithm, the credential classifier (`credentials.py`, which
decides SigV4 vs bearer vs both), and the namespace mapping (`namespace.py`,
subject<->bucket and key<->path with the reserved ``shared/`` prefix). Nothing
in this package touches a framework or a database; the verifier is testable
against AWS's published vectors alone and the mapping against plain strings.
"""

from __future__ import annotations

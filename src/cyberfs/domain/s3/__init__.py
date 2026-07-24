"""S3-compatible surface: access-key credentials and, later, SigV4.

The domain here owns the credential model -- an access key resolves to an
existing CyberdyneAuth subject and carries no identity of its own -- and the
pure signing algorithm. Nothing in this package touches a framework or a
database; the verifier is testable against AWS's published vectors alone.
"""

from __future__ import annotations

"""WebDAV property mapping and multistatus XML.

Pure: no HTTP, no repository, no I/O. A `PROPFIND` response is a function of the
nodes it describes, so it is built and tested here rather than inside a router.

The properties are the ones a file manager reads to render a tree. Every one is
derived from stored metadata -- nothing here opens content, and nothing here can,
which is what keeps the encryption story intact on a third surface.
"""

from __future__ import annotations

from datetime import datetime
from email.utils import format_datetime
from urllib.parse import quote
from xml.sax.saxutils import escape

from cyberfs.domain.nodes import Node

DAV_NAMESPACE = "DAV:"
#: Class 1 only. Class 2 would mean LOCK/UNLOCK, and CyberFS has no lock concept
#: -- see `webdav-compatibility/spec.md`, "Locking is refused rather than faked".
DAV_COMPLIANCE = "1"
#: Advertised by OPTIONS. Exactly what is implemented: a client that reads this
#: and tries something else has been misled by us, not by its own guesswork.
ALLOWED_METHODS = (
    "OPTIONS",
    "PROPFIND",
    "GET",
    "HEAD",
    "PUT",
    "DELETE",
    "MKCOL",
    "COPY",
    "MOVE",
)
#: `Depth: infinity` is a recursive walk of an unbounded subtree in one request,
#: which is how a WebDAV server is made to exhaust itself. RFC 4918 permits
#: refusing it, and most servers do.
SUPPORTED_DEPTHS = ("0", "1")

_MULTISTATUS_OPEN = (
    f'<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="{DAV_NAMESPACE}">'
)
_MULTISTATUS_CLOSE = "</D:multistatus>"


def href_for(base_path: str, path: str, *, is_collection: bool) -> str:
    """The URL a client uses to address a node.

    Percent-encoded per segment, so a name containing a space or a `#` addresses
    the node it names rather than truncating the path. A collection ends in a
    slash: several clients treat its absence as "this is a file" regardless of
    what `resourcetype` said.
    """
    segments = [quote(part, safe="") for part in path.split("/") if part]
    href = "/".join([base_path.rstrip("/"), *segments])
    return f"{href}/" if is_collection and not href.endswith("/") else href


def _http_date(moment: datetime) -> str:
    return format_datetime(moment, usegmt=True)


def _prop_block(node: Node) -> str:
    """The properties for one node, in the order clients expect to find them."""
    if node.is_folder:
        resourcetype = "<D:resourcetype><D:collection/></D:resourcetype>"
        # A collection has no length or content type. Reporting 0 bytes would be
        # a claim about content it does not have.
        specific = ""
    else:
        resourcetype = "<D:resourcetype/>"
        content_type = escape(node.content_type or "application/octet-stream")
        specific = (
            f"<D:getcontentlength>{node.size_bytes}</D:getcontentlength>"
            f"<D:getcontenttype>{content_type}</D:getcontenttype>"
        )
    return (
        f"<D:displayname>{escape(node.name)}</D:displayname>"
        f"{resourcetype}"
        f"{specific}"
        f"<D:getlastmodified>{_http_date(node.updated_at)}</D:getlastmodified>"
        # The node's own ETag verbatim. A client that caches on one surface and
        # revalidates on another must not be told the same state has two tags.
        f"<D:getetag>{escape(node.etag)}</D:getetag>"
    )


def response_for(base_path: str, node: Node, path: str) -> str:
    """One `<D:response>` describing `node` at `path`."""
    href = escape(href_for(base_path, path, is_collection=node.is_folder))
    return (
        "<D:response>"
        f"<D:href>{href}</D:href>"
        "<D:propstat>"
        f"<D:prop>{_prop_block(node)}</D:prop>"
        "<D:status>HTTP/1.1 200 OK</D:status>"
        "</D:propstat>"
        "</D:response>"
    )


def multistatus(base_path: str, entries: list[tuple[Node, str]]) -> str:
    """A `207 Multi-Status` body describing each `(node, path)` in turn."""
    responses = "".join(response_for(base_path, node, path) for node, path in entries)
    return f"{_MULTISTATUS_OPEN}{responses}{_MULTISTATUS_CLOSE}"


def error_body(status: int, reason: str) -> str:
    """A WebDAV-shaped error, never the REST problem document.

    A client that asked for XML and got JSON reports a parse failure instead of
    the reason, which is the least useful outcome available.
    """
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<D:error xmlns:D="{DAV_NAMESPACE}">'
        f"<D:status>HTTP/1.1 {status} {escape(reason)}</D:status>"
        "</D:error>"
    )

# WebDAV, and mounting CyberFS as a filesystem

CyberFS serves a **WebDAV Class 1** surface at `WEBDAV_BASE_PATH` (default
`/webdav`). It is **on by default** — `WEBDAV_ENABLED=false` removes the routes
entirely.

This is also how CyberFS becomes a FUSE filesystem: `rclone mount` over WebDAV is
a FUSE mount. There is no driver to install beyond `rclone` itself, and none in
this repository.

## Credentials

WebDAV authenticates with an **existing S3 access key** over HTTP Basic — the key
id is the username, the secret is the password. Mint one over REST:

```sh
curl -X POST https://cyberfs.example/api/v1/me/s3-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"label":"my-laptop"}'
```

The secret is returned **once**. Revoke it with
`DELETE /api/v1/me/s3-keys/{key_id}`; the next WebDAV request is refused
immediately, with no cache to expire.

A key names a subject and carries no permission of its own, so what it can reach
is exactly what that subject can reach. **It never confers administrator rights**,
by construction — a leaked key cannot be used to reach the admin surface.

## TLS is required

Basic authentication sends the secret on **every request**. Outside local and test
environments the surface **refuses a plaintext request** with `403`. Put TLS in
front of it, or set `WEBDAV_ENABLED=false`.

If you terminate TLS at a proxy, that proxy must set `X-Forwarded-Proto: https`
or every request is refused.

## rclone

```sh
rclone config create cyberfs webdav \
  url=https://cyberfs.example/webdav \
  vendor=other \
  user=$ACCESS_KEY_ID \
  pass="$(rclone obscure "$SECRET_ACCESS_KEY")"

rclone lsd cyberfs:                      # folders
rclone copy ./report.pdf cyberfs:papers/ # upload
rclone cat cyberfs:papers/report.pdf     # download
```

### The FUSE mount

```sh
mkdir -p ~/cyberfs
rclone mount cyberfs: ~/cyberfs --vfs-cache-mode writes
```

`~/cyberfs` is now a filesystem. `--vfs-cache-mode writes` matters: without it,
applications that open a file for random-access writing fail, because WebDAV has
no partial-write operation.

## davfs2

```sh
sudo mount -t davfs https://cyberfs.example/webdav /mnt/cyberfs
# username: the access key id, password: the secret
```

## Finder and Windows Explorer — read-only in practice

Both mount the surface, and both will usually **refuse to write** to it.

They require WebDAV **Class 2**, which means `LOCK`/`UNLOCK`. CyberFS implements
Class 1 and refuses those methods with `405` rather than pretending: it has no lock
concept, its concurrency control is optimistic (`If-Match` against a node
revision), and a lock that did not actually prevent a concurrent REST or S3 write
would be a lie told to the client. `OPTIONS` advertises `DAV: 1` honestly so a
client knows before it tries.

Use `rclone` for read-write access from a desktop.

## What is not implemented

| Method | Behaviour | Why |
| --- | --- | --- |
| `LOCK`, `UNLOCK` | `405` | No lock concept; see above |
| `PROPPATCH` | `405` | Tags and metadata are the right home for caller-defined properties, but mapping dead properties onto them is its own design |
| `PROPFIND` `Depth: infinity` | `403` | An unbounded recursive walk in one request |

## How it relates to the other surfaces

Every WebDAV method delegates to the same use case its REST equivalent calls, so a
byte written over WebDAV is indistinguishable from one written over REST or S3:

- quotas are charged identically, and an over-quota `PUT` returns `507`
- a folder's encryption default applies to a WebDAV upload
- `DELETE` is a **soft delete** — the node goes to the trash and stays restorable
- operations appear in the caller's activity and the audit log
- the `getetag` property is the REST `ETag`, unchanged, so a client caching across
  surfaces is not misled

Trashed nodes are absent from listings and unaddressable, and a path always
resolves from the caller's own root, so it cannot reach another user's tree.

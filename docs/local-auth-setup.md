# Running CyberdyneAuth locally for CyberFS development

Reproduces the setup used to provision the CyberFS OAuth client. Assumes a
CyberdyneAuth checkout at `../CyberdyneAuth`.

Ports are shifted off the defaults (8000/5432) because other Cyberdyne
projects commonly hold them.

## 1. RSA keypair

RS256 is mandatory — see the prerequisite section in
[auth-integration.md](auth-integration.md).

```bash
cd ../CyberdyneAuth
mkdir -p .local-keys
openssl genrsa -out .local-keys/jwt-private.pem 2048
openssl rsa -in .local-keys/jwt-private.pem -pubout -out .local-keys/jwt-public.pem
```

## 2. `.env`

```ini
APP_ENV=dev                      # NOT "development" — the literal is dev|staging|production
APP_PORT=8001
DATABASE_URL=postgresql+asyncpg://cyberdyne:cyberdyne@localhost:5433/cyberdyne_auth

JWT_ALGORITHM=RS256
JWT_PRIVATE_KEY_PATH=.local-keys/jwt-private.pem
JWT_PUBLIC_KEY_PATH=.local-keys/jwt-public.pem
JWT_KEY_ID=local-dev-key-1
JWT_SECRET_KEY=local-dev-secret-not-used-under-rs256-but-still-required

OIDC_ENABLED=true
OIDC_ISSUER=http://localhost:8001

# Must decode to exactly 32 bytes.
MASTER_KEY=bG9jYWwtZGV2LW1hc3Rlci1rZXktMzItYnl0ZXMtb2s=

# Must be a routable domain: the email validator rejects reserved TLDs
# such as .local and .test.
ADMIN_EMAIL_LIST=admin@example.com

HOOKS_ENABLED=false
BLOCKCHAIN_ENABLED=false
BILLING_ENABLED=false
```

## 3. Database and migrations

```bash
docker run -d --name cyberfs-auth-db \
  -e POSTGRES_USER=cyberdyne -e POSTGRES_PASSWORD=cyberdyne \
  -e POSTGRES_DB=cyberdyne_auth -p 5433:5432 postgres:16-alpine

cd ../CyberdyneAuth
uv sync
uv run alembic upgrade head
uv run uvicorn cyberdyne_auth.adapters.inbound.api.app:app --host 127.0.0.1 --port 8001
```

Confirm discovery and JWKS are live:

```bash
curl -s http://127.0.0.1:8001/.well-known/openid-configuration | jq .issuer
curl -s http://127.0.0.1:8001/.well-known/jwks.json | jq '.keys | length'
```

## 4. Admin user and client

```bash
B=http://127.0.0.1:8001
curl -s -X POST "$B/api/v1/auth/register" -H 'content-type: application/json' \
  -d '{"email":"admin@example.com","password":"<pw>","full_name":"Local Admin"}'

ADMIN_TOKEN=$(curl -s -X POST "$B/api/v1/auth/login" -H 'content-type: application/json' \
  -d '{"email":"admin@example.com","password":"<pw>"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

bash ../CyberdyneAuth/.claude/skills/provision-oauth-client/provision_client.sh \
  --base-url "$B" --admin-token "$ADMIN_TOKEN" \
  --name cyberfs --type confidential \
  --grants client_credentials --scopes openid,directory:read \
  --trusted --secret-out ./.cyberfs-client.secret
```

`directory:read` is the service-to-service user-picker scope; CyberFS needs it
to resolve share recipients by email. The script prints only the `client_id` —
the secret goes to a mode-600 file, which `.gitignore` covers.

## 5. Point CyberFS at it

```ini
CYBERDYNE_AUTH_BASE_URL=http://localhost:8001
CYBERFS_CLIENT_ID=<printed client_id>
CYBERFS_CLIENT_SECRET=<contents of .cyberfs-client.secret>
```

## 6. Verify end to end

```bash
CYBERFS_LIVE_AUTH_BASE_URL=http://localhost:8001 \
CYBERFS_LIVE_CLIENT_ID=<client_id> \
CYBERFS_LIVE_CLIENT_SECRET="$(cat .cyberfs-client.secret)" \
CYBERFS_LIVE_USER_TOKEN="<a user access token>" \
uv run pytest tests/integration/test_live_auth.py -m integration
```

## Teardown

```bash
docker rm -f cyberfs-auth-db
```

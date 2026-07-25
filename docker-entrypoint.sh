#!/bin/sh
# API container entrypoint.
#
# Apply outstanding Alembic migrations, then -- and only then -- bind the HTTP
# port. `set -e` means a failed migration exits non-zero and the container
# never serves traffic against a half-migrated schema (deployment/spec.md).
# Concurrent replica starts serialize on a Postgres advisory lock inside the
# migrator, so exactly one applies while the others wait.
set -e

echo "cyberfs: applying migrations"
python -m cyberfs.infrastructure.migrate

echo "cyberfs: starting api"
exec uvicorn cyberfs.adapters.inbound.api.app:create_app \
    --factory \
    --host 0.0.0.0 \
    --port "${PORT:-8000}"

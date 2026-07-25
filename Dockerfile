# CyberFS API image -- multi-stage, non-root, pinned bases, dev/test excluded.
#
# IMPORTANT layout note: cyberfs.infrastructure.migrate resolves the repo root
# as parents[3] of its own file and loads alembic.ini + alembic/ from there. So
# the image runs from a source-tree layout (/app/src/cyberfs/..., /app/alembic,
# /app/alembic.ini) with PYTHONPATH=/app/src -- NOT a site-packages install,
# which would break that path arithmetic.

# ---- builder: resolve third-party deps into a venv from the committed lock ---
FROM ghcr.io/astral-sh/uv:0.5.11-python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Only the lock inputs, so this layer caches unless dependencies change.
# --no-install-project + --no-dev => no pytest/ruff/mypy/respx, no app code.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ---- runtime ----------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# postgresql-client provides pg_dump/pg_restore for the backup/restore paths.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user (spec: runs as non-root).
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin appuser

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

WORKDIR /app

# Third-party deps from the builder, then the source tree. Note what is NOT
# copied: tests/, admin/, docs/, .env -- see .dockerignore.
COPY --from=builder /app/.venv /app/.venv
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER appuser

EXPOSE 8000

# Liveness only -- no curl in the base image, so use the interpreter.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health/live').status==200 else 1)"

ENTRYPOINT ["docker-entrypoint.sh"]

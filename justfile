# CyberFS -- recipes mirror the CyberdyneAuth conventions.

default:
    @just --list

# --- setup -------------------------------------------------------------

install:
    uv sync --all-extras
    uv run pre-commit install

# --- local stack -------------------------------------------------------

db-up:
    docker compose up -d postgres redis minio

db-wait:
    docker compose exec -T postgres sh -c 'until pg_isready -U cyberfs -q; do sleep 1; done'

up:
    docker compose up -d

up-build:
    docker compose up -d --build

stop:
    docker compose stop

down:
    docker compose down

# Destructive: wipes Postgres and MinIO volumes.
reset:
    docker compose down -v

logs service="":
    docker compose logs -f {{service}}

psql:
    docker compose exec postgres psql -U cyberfs -d cyberfs

# --- serve -------------------------------------------------------------

# Canonical zero-to-running command.
dev: db-up db-wait migrate serve-dev

serve-dev:
    uv run uvicorn cyberfs.adapters.inbound.api.app:create_app --factory --reload --host 0.0.0.0 --port 8000

serve:
    uv run uvicorn cyberfs.adapters.inbound.api.app:create_app --factory --host 0.0.0.0 --port 8000

# --- migrations --------------------------------------------------------

migrate:
    uv run alembic upgrade head

migrate-down:
    uv run alembic downgrade -1

migration-status:
    uv run alembic current

make-migration message:
    uv run alembic revision --autogenerate -m "{{message}}"

# --- tests -------------------------------------------------------------

test:
    uv run pytest

test-unit:
    uv run pytest tests/unit

test-integration:
    uv run pytest tests/integration -m integration

test-cov:
    uv run pytest tests/unit --cov --cov-report=term-missing --cov-report=html

# --- quality gate ------------------------------------------------------

lint:
    uv run ruff check .

lint-fix:
    uv run ruff check --fix .

fmt:
    uv run ruff format .

typecheck:
    uv run mypy

check: lint typecheck test-cov

# --- misc --------------------------------------------------------------

openapi:
    uv run python -c "import json; from cyberfs.adapters.inbound.api.app import create_app; print(json.dumps(create_app().openapi(), indent=2))"

spec-validate:
    openspec validate --all --strict

clean:
    rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage

ci: lint typecheck test-cov spec-validate

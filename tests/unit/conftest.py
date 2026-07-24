"""Shared fixtures for the unit suite."""

from __future__ import annotations

import base64
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cyberfs.adapters.inbound.api.app import create_app
from cyberfs.infrastructure.settings import Environment, Settings

TEST_MASTER_KEY = base64.b64encode(b"\x07" * 32).decode()


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "environment": Environment.TEST,
        "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
        "redis_url": "redis://localhost:6379/0",
        "minio_endpoint": "localhost:9000",
        "minio_access_key": "key",
        "minio_secret_key": "unit-test-minio-value",
        "minio_bucket": "cyberfs-content",
        "cyberdyne_auth_base_url": "https://auth.example.test",
        "cyberfs_client_id": "cyberfs",
        "cyberfs_client_secret": "unit-test-client-value",
        "master_key": TEST_MASTER_KEY,
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client

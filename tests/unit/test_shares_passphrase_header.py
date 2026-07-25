"""Regression tests for the X-Link-Passphrase access-header length cap.

The create-side body caps the passphrase at 255 chars (schemas.py); the
public link-access header must match, so an over-length value is rejected at
request validation (422) *before* scrypt runs. A valid-length wrong
passphrase must still reach verification and be denied (403).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from http import HTTPStatus

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cyberfs.adapters.inbound.api.dependencies import unit_of_work
from cyberfs.domain.errors import PermissionDeniedError

from .conftest import make_settings


class _RecordingSharing:
    """Stands in for SharingService, recording whether resolve_link ran."""

    def __init__(self) -> None:
        self.resolve_called = False

    async def resolve_link(self, *_args: object, **_kwargs: object) -> object:
        self.resolve_called = True
        raise PermissionDeniedError("the link passphrase is incorrect")


@pytest.fixture
def app_and_stub() -> Iterator[tuple[FastAPI, _RecordingSharing]]:
    from cyberfs.adapters.inbound.api.app import create_app

    app = create_app(make_settings())
    stub = _RecordingSharing()
    app.state.sharing = stub

    async def _override() -> AsyncIterator[object]:
        yield object()

    app.dependency_overrides[unit_of_work] = _override
    yield app, stub
    app.dependency_overrides.clear()


def test_over_length_passphrase_header_rejected_before_verify(
    app_and_stub: tuple[FastAPI, _RecordingSharing],
) -> None:
    app, stub = app_and_stub
    with TestClient(app) as client:
        resp = client.get("/api/v1/public/tok", headers={"X-Link-Passphrase": "p" * 300})
    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert stub.resolve_called is False  # scrypt never reached


def test_valid_length_wrong_passphrase_still_denied(
    app_and_stub: tuple[FastAPI, _RecordingSharing],
) -> None:
    app, stub = app_and_stub
    with TestClient(app) as client:
        resp = client.get("/api/v1/public/tok", headers={"X-Link-Passphrase": "wrong-but-short"})
    assert resp.status_code == HTTPStatus.FORBIDDEN
    assert stub.resolve_called is True

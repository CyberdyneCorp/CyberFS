"""Settings validation and `.env.example` drift."""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from cyberfs.infrastructure.settings import (
    DEV_MASTER_KEY_PLACEHOLDER,
    Environment,
    MasterKeyError,
    Settings,
    decode_master_key,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
COMPOSE_COOLIFY = REPO_ROOT / "compose.coolify.yaml"

# Sentinels are distinctive so a leak test cannot pass by accident -- "secret"
# alone would match the *field name* `minio_secret_key` in a repr.
MINIO_SECRET_SENTINEL = "m1n10-Sentinel-Value"
CLIENT_SECRET_SENTINEL = "cl13nt-Sentinel-Value"

REQUIRED = {
    "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
    "redis_url": "redis://localhost:6379/0",
    "minio_endpoint": "localhost:9000",
    "minio_access_key": "key",
    "minio_secret_key": MINIO_SECRET_SENTINEL,
    "minio_bucket": "cyberfs-content",
    "cyberdyne_auth_base_url": "https://auth.example.test",
    "cyberfs_client_id": "cyberfs",
    "cyberfs_client_secret": CLIENT_SECRET_SENTINEL,
    "master_key": DEV_MASTER_KEY_PLACEHOLDER,
}

# A well-formed key that is not the placeholder, for production-environment cases.
REAL_MASTER_KEY = base64.b64encode(b"\x01" * 32).decode()


def build(**overrides: object) -> Settings:
    # `_env_file=None`: never read the developer's local .env in a test.
    return Settings(**{**REQUIRED, "_env_file": None, **overrides})  # type: ignore[arg-type]


# --- .env.example stays in sync -------------------------------------------


def env_example_keys() -> set[str]:
    pattern = re.compile(r"^([A-Z][A-Z0-9_]*)=")
    return {
        match.group(1)
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if (match := pattern.match(line))
    }


def test_env_example_documents_every_setting() -> None:
    documented = env_example_keys()
    expected = {name.upper() for name in Settings.model_fields}
    assert expected - documented == set(), "settings missing from .env.example"


def test_env_example_has_no_unknown_keys() -> None:
    documented = env_example_keys()
    expected = {name.upper() for name in Settings.model_fields}
    assert documented - expected == set(), ".env.example documents settings that do not exist"


# --- required configuration ------------------------------------------------


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_missing_required_setting_fails_startup(missing: str) -> None:
    values = {k: v for k, v in REQUIRED.items() if k != missing}
    with pytest.raises((ValidationError, MasterKeyError)) as exc:
        Settings(**values, _env_file=None)  # type: ignore[arg-type]
    assert missing in str(exc.value).lower()


# --- master key ------------------------------------------------------------


def test_master_key_decodes_to_32_bytes() -> None:
    assert len(build().master_key_bytes) == 32


def test_master_key_rejects_non_base64() -> None:
    # pydantic wraps the MasterKeyError raised inside the model validator.
    with pytest.raises(ValidationError, match="base64"):
        build(master_key="not base64 at all !!!")


def test_master_key_rejects_wrong_length() -> None:
    short = base64.b64encode(b"too-short").decode()
    with pytest.raises(ValidationError, match="32 bytes"):
        build(master_key=short)


def test_master_key_error_never_contains_the_value() -> None:
    """`content-encryption/spec.md`: key material never reaches an error path."""
    secret = "c3VwZXItc2VjcmV0LWtleS12YWx1ZQ=="  # valid base64, wrong length
    with pytest.raises(MasterKeyError) as exc:
        decode_master_key(secret)
    assert secret not in str(exc.value)


def test_production_rejects_the_development_placeholder() -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        build(environment=Environment.PRODUCTION, master_key=DEV_MASTER_KEY_PLACEHOLDER)


def test_production_accepts_a_real_key() -> None:
    assert build(environment=Environment.PRODUCTION, master_key=REAL_MASTER_KEY).is_production


def test_rotation_exposes_both_keys() -> None:
    previous = base64.b64encode(b"\x02" * 32).decode()
    settings = build(master_key_previous=previous)
    assert settings.rotation_in_progress
    assert settings.master_key_previous_bytes == b"\x02" * 32


def test_no_rotation_by_default() -> None:
    settings = build()
    assert not settings.rotation_in_progress
    assert settings.master_key_previous_bytes is None


# --- dev mode --------------------------------------------------------------


@pytest.mark.parametrize("env", [Environment.LOCAL, Environment.TEST])
def test_dev_mode_allowed_outside_deployed_environments(env: Environment) -> None:
    assert build(environment=env, auth_dev_mode=True).auth_dev_mode


@pytest.mark.parametrize("env", [Environment.STAGING, Environment.PRODUCTION])
def test_dev_mode_rejected_in_deployed_environments(env: Environment) -> None:
    # A real key, so this exercises the dev-mode rule rather than tripping the
    # placeholder check first.
    with pytest.raises(ValidationError, match="AUTH_DEV_MODE"):
        build(environment=env, auth_dev_mode=True, master_key=REAL_MASTER_KEY)


# --- schedules -------------------------------------------------------------


@pytest.mark.parametrize("field", ["backup_cron", "rewrap_cron"])
@pytest.mark.parametrize("expression", ["", "   ", "disabled", "never", "0 3 * *", "99 3 * * *"])
def test_an_unparseable_cron_refuses_to_boot(field: str, expression: str) -> None:
    """Regression: a bad schedule used to be accepted and fail hours later.

    `seconds_until_next` parses on every tick, so the failure surfaced as a
    background task that had stopped -- the service healthy, the job silently
    never running, and nothing wrong until a staleness alert.
    """
    with pytest.raises(ValidationError, match="cron"):
        build(**{field: expression})


@pytest.mark.parametrize("expression", ["0 3 * * *", "*/2 * * * *", "0 0 1 * 0", "15,45 * * * 1-5"])
def test_valid_cron_expressions_are_accepted(expression: str) -> None:
    assert build(backup_cron=expression).backup_cron == expression


def test_the_shipped_defaults_are_valid() -> None:
    settings = build()
    assert settings.backup_cron == "0 3 * * *"
    assert settings.rewrap_cron == "*/2 * * * *"


# --- backup target ---------------------------------------------------------


def test_backup_disabled_needs_no_target() -> None:
    assert not build(backup_enabled=False).backup_enabled


def test_backup_enabled_requires_full_target() -> None:
    with pytest.raises(ValidationError, match="BACKUP_S3_ENDPOINT"):
        build(backup_enabled=True)


def test_backup_target_may_not_be_the_primary() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        build(
            backup_enabled=True,
            backup_s3_endpoint=REQUIRED["minio_endpoint"],
            backup_s3_bucket=REQUIRED["minio_bucket"],
            backup_s3_access_key="k",
            backup_s3_secret_key="s",
        )


def test_backup_tls_follows_the_primary_when_unset() -> None:
    settings = build(
        backup_enabled=True,
        minio_secure=False,
        backup_s3_endpoint="backup.example:9000",
        backup_s3_bucket="cyberfs-backup",
        backup_s3_access_key="k",
        backup_s3_secret_key="s",
    )
    assert settings.backup_target_secure is False


def test_backup_tls_is_independent_of_the_primary() -> None:
    """The realistic shape: internal primary in plaintext, off-site target on TLS.

    Reusing MINIO_SECURE for both made an off-site backup impossible to reach
    over https whenever the primary was internal.
    """
    settings = build(
        backup_enabled=True,
        minio_secure=False,
        backup_s3_secure=True,
        backup_s3_endpoint="backup.example.com",
        backup_s3_bucket="cyberfs-backup",
        backup_s3_access_key="k",
        backup_s3_secret_key="s",
    )
    assert settings.minio_secure is False
    assert settings.backup_target_secure is True


def test_backup_target_on_same_host_different_bucket_is_allowed() -> None:
    settings = build(
        backup_enabled=True,
        backup_s3_endpoint=REQUIRED["minio_endpoint"],
        backup_s3_bucket="cyberfs-backup",
        backup_s3_access_key="k",
        backup_s3_secret_key="s",
    )
    assert settings.backup_s3_bucket == "cyberfs-backup"


# --- misc ------------------------------------------------------------------


def test_page_size_default_may_not_exceed_max() -> None:
    with pytest.raises(ValidationError, match="PAGE_SIZE_DEFAULT"):
        build(page_size_default=2000, page_size_max=1000)


def test_secrets_are_not_reprd() -> None:
    """Secrets must not leak through a repr, which is what startup logging prints."""
    rendered = repr(build())
    assert CLIENT_SECRET_SENTINEL not in rendered
    assert MINIO_SECRET_SENTINEL not in rendered
    assert DEV_MASTER_KEY_PLACEHOLDER not in rendered


# --- the deployment can actually set what the docs document ----------------
#
# `.env.example` being complete is necessary and was not sufficient. A Compose
# service only receives the variables its own `environment:` block names, so a
# setting documented in `.env.example` and absent from `compose.coolify.yaml` is
# one an operator can set in Coolify to no effect whatsoever. Fifty-three of the
# seventy-four settings were in that state, including `MASTER_KEY_PREVIOUS` --
# which made the documented online key-rotation procedure impossible to perform
# on the deployment it documents.


def compose_environment_keys() -> set[str]:
    """Variables the `api` service in the Coolify stack receives.

    Parsed with a regex rather than a YAML loader on purpose: the values contain
    `${VAR:-default}` interpolations that are Compose syntax, and the point here
    is only which keys are named.
    """
    text = COMPOSE_COOLIFY.read_text(encoding="utf-8")
    api = text.split("\n  dashboard:", 1)[0]
    # List form, deliberately. `- KEY` passes a variable through only when it is
    # set; the mapping form `KEY: ${KEY:-}` always defines the key as an empty
    # string, and 49 of these settings are non-optional and cannot parse "".
    # That distinction took the deployment down once.
    return set(re.findall(r"^\s{6,}-\s*([A-Z][A-Z0-9_]+)(?:=|$)", api, re.M))


def test_the_coolify_stack_can_set_every_documented_setting() -> None:
    """Every setting reaches the container, or an operator cannot configure it.

    `AUTH_DEV_MODE` is deliberately excluded: `Settings` refuses it outside
    local/test, so passing it through would only offer a knob that cannot be
    turned. `ENVIRONMENT` is pinned to `production` in the file rather than
    interpolated.
    """
    reachable = compose_environment_keys()
    expected = {name.upper() for name in Settings.model_fields} - {"AUTH_DEV_MODE"}

    unreachable = expected - reachable
    assert unreachable == set(), "settings an operator cannot set on the deployment: " + ", ".join(
        sorted(unreachable)
    )


def test_the_coolify_stack_passes_nothing_that_is_not_a_setting() -> None:
    """The other direction, so a typo is caught rather than silently ignored.

    A misspelled key in the compose block is dead weight that looks like
    configuration, and pydantic-settings ignores it without complaint.
    """
    reachable = compose_environment_keys()
    expected = {name.upper() for name in Settings.model_fields}
    # Container plumbing that is not a CyberFS setting.
    infrastructure = {
        "POSTGRES_PASSWORD",
        "POSTGRES_USER",
        "POSTGRES_DB",
        "MINIO_ROOT_USER",
        "MINIO_ROOT_PASSWORD",
        "NODE_ENV",
        "HOST",
        "PORT",
    }

    stray = reachable - expected - infrastructure
    assert stray == set(), "compose passes variables that are not settings: " + ", ".join(
        sorted(stray)
    )

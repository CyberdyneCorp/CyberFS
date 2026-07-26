"""Runtime configuration.

Every setting comes from the environment and is validated at startup. The
service refuses to boot on invalid configuration rather than failing later,
per request -- see `deployment/spec.md`, "Configuration".
"""

from __future__ import annotations

import base64
import binascii
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from cyberfs.domain.schedule import CronError, validate_cron

MASTER_KEY_BYTES = 32

# Shipped in .env.example and docker-compose so a fresh clone runs. Rejected in
# production: a deployment that boots with this key is not protecting anything.
# base64 of b"dev-only-master-key-do-not-use!!"
DEV_MASTER_KEY_PLACEHOLDER = "ZGV2LW9ubHktbWFzdGVyLWtleS1kby1ub3QtdXNlISE="


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class MasterKeyError(ValueError):
    """MASTER_KEY is missing, malformed, or unsafe for this environment."""


def decode_master_key(raw: str) -> bytes:
    """Decode a base64 MASTER_KEY into exactly 32 bytes.

    Never include the value in the error -- see `content-encryption/spec.md`.
    """
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MasterKeyError("MASTER_KEY is not valid base64") from exc
    if len(key) != MASTER_KEY_BYTES:
        raise MasterKeyError(f"MASTER_KEY must decode to {MASTER_KEY_BYTES} bytes, got {len(key)}")
    return key


PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class Settings(BaseSettings):
    """Flat environment configuration. Grouped by capability for readability."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- runtime -------------------------------------------------------
    environment: Environment = Environment.LOCAL
    log_level: str = "INFO"
    api_root_path: str = ""
    cors_allowed_origins: list[str] = Field(default_factory=list)
    metrics_enabled: bool = True

    # --- datastores ----------------------------------------------------
    database_url: str
    database_pool_size: PositiveInt = 10
    database_max_overflow: NonNegativeInt = 5
    redis_url: str

    minio_endpoint: str
    minio_access_key: SecretStr
    minio_secret_key: SecretStr
    minio_bucket: str
    minio_secure: bool = True
    minio_region: str | None = None

    # --- identity (CyberdyneAuth) --------------------------------------
    cyberdyne_auth_base_url: str
    cyberfs_client_id: str
    cyberfs_client_secret: SecretStr
    oidc_discovery_ttl_seconds: PositiveInt = 3600
    jwks_stale_max_seconds: PositiveInt = 86400
    jwks_refresh_cooldown_seconds: PositiveInt = 60
    token_clock_skew_seconds: NonNegativeInt = 60
    ratelimit_auth_failures_per_min: NonNegativeInt = 30
    # Accepts a stub principal so the stack is usable without a live auth
    # service. Rejected outside local/test -- see `deployment/spec.md`.
    auth_dev_mode: bool = False

    # --- encryption ----------------------------------------------------
    master_key: SecretStr
    # Previous key, set only while a MASTER_KEY rotation is in progress.
    master_key_previous: SecretStr | None = None
    encryption_default_on: bool = False
    encryption_frame_bytes: PositiveInt = 64 * 1024
    async_rewrap_threshold_nodes: PositiveInt = 100

    # --- filesystem ----------------------------------------------------
    default_quota_bytes: PositiveInt = 10 * 1024**3
    max_upload_bytes: PositiveInt = 5 * 1024**3
    upload_chunk_bytes: PositiveInt = 8 * 1024 * 1024
    max_tree_depth: PositiveInt = 64
    page_size_default: PositiveInt = 100
    page_size_max: PositiveInt = 1000
    version_retention_count: PositiveInt = 10
    trash_retention_days: PositiveInt = 30
    orphan_grace_minutes: PositiveInt = 60

    # --- cache ---------------------------------------------------------
    cache_ttl_permission_seconds: PositiveInt = 60
    cache_ttl_metadata_seconds: PositiveInt = 300
    cache_ttl_listing_seconds: PositiveInt = 120
    cache_ttl_jwks_seconds: PositiveInt = 3600
    cache_ttl_quota_seconds: PositiveInt = 300
    cache_ttl_admin_stats_seconds: PositiveInt = 600
    cache_op_timeout_ms: PositiveInt = 50
    cache_circuit_trip_seconds: PositiveInt = 10
    cache_schema_version: PositiveInt = 1

    # --- admin ---------------------------------------------------------
    admin_show_filenames: bool = False

    # --- sharing -------------------------------------------------------
    public_link_max_attempts_per_min: NonNegativeInt = 10
    # How often the background worker completes the deferred rewrap of large
    # shared subtrees (those over `ASYNC_REWRAP_THRESHOLD_NODES`). A pending
    # share confers no access until this worker rewraps every encrypted
    # descendant for the recipient, so the cadence bounds how long a large share
    # takes to become usable. See `sharing/spec.md`, "Async rewrap for large
    # subtrees".
    rewrap_cron: str = "*/2 * * * *"

    # --- activity ------------------------------------------------------
    # How long operation (activity) records are kept before the prune job
    # deletes them. Security records -- denials, grants, transfers, encryption
    # changes, admin actions -- are retained separately and are never pruned
    # here. See `activity-reporting/spec.md`.
    activity_retention_days: PositiveInt = 90
    # The longest window `GET /api/v1/me/activity` will answer; a longer request
    # is refused with 422 rather than scanning an unbounded range.
    activity_max_window_days: PositiveInt = 90

    # --- s3 compatibility ----------------------------------------------
    # The AWS region a SigV4 credential scope must name. S3 clients sign with a
    # region and CyberFS reproduces the same canonical request to verify, so
    # this is part of the signing contract rather than a routing hint.
    s3_region: str = "us-east-1"
    # How far a signed `x-amz-date` may sit from the server clock before the
    # request is refused with `RequestTimeTooSkewed`. Bounds replay of a
    # captured signature to this window.
    s3_clock_skew_seconds: NonNegativeInt = 300
    # The S3 HTTP surface is mounted only when enabled; a deployment that does
    # not offer S3 exposes no such routes at all.
    s3_api_enabled: bool = False
    # The path the S3 surface is rooted at. Standard clients point their
    # endpoint URL here; buckets and keys hang off it.
    s3_base_path: str = "/s3"
    # How long a multipart upload may sit neither completed nor aborted before
    # the orphan reaper reclaims its staged parts. Bounds the storage a
    # half-finished upload can occupy.
    s3_multipart_abandon_hours: PositiveInt = 24
    # The externally visible CyberFS S3 base URL (scheme + host, e.g.
    # `https://s3.cyberfs.example`). Presigned URLs and the multipart
    # `<Location>` are rooted here so they address CyberFS's own endpoint and
    # never the underlying object store. When unset the multipart `<Location>`
    # falls back to the request host; issuing a presigned URL requires it.
    s3_public_endpoint: str | None = None

    # --- backup --------------------------------------------------------
    backup_enabled: bool = False
    backup_cron: str = "0 3 * * *"
    backup_s3_endpoint: str | None = None
    backup_s3_access_key: SecretStr | None = None
    backup_s3_secret_key: SecretStr | None = None
    backup_s3_bucket: str | None = None
    # TLS for the *backup* target, independent of the primary store. The two
    # normally differ: the primary is reached over an internal network in
    # plaintext, while an off-site target is public and therefore TLS. Unset
    # follows `MINIO_SECURE`, which is what a same-host target wants.
    backup_s3_secure: bool | None = None
    backup_verify_sample_count: PositiveInt = 50
    backup_keep_daily: NonNegativeInt = 7
    backup_keep_weekly: NonNegativeInt = 4
    backup_keep_monthly: NonNegativeInt = 6
    backup_failed_grace_hours: PositiveInt = 24
    backup_max_age_hours: PositiveInt = 48
    backup_history_days: PositiveInt = 90

    # --- derived -------------------------------------------------------

    @property
    def backup_target_secure(self) -> bool:
        """Whether the backup target speaks TLS. Falls back to the primary."""
        return self.minio_secure if self.backup_s3_secure is None else self.backup_s3_secure

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def master_key_bytes(self) -> bytes:
        return decode_master_key(self.master_key.get_secret_value())

    @property
    def master_key_previous_bytes(self) -> bytes | None:
        if self.master_key_previous is None:
            return None
        return decode_master_key(self.master_key_previous.get_secret_value())

    @property
    def rotation_in_progress(self) -> bool:
        return self.master_key_previous is not None

    # --- validation ----------------------------------------------------

    @model_validator(mode="after")
    def _validate_master_key(self) -> Self:
        decode_master_key(self.master_key.get_secret_value())
        if self.master_key_previous is not None:
            decode_master_key(self.master_key_previous.get_secret_value())
        if self.is_production and self.master_key.get_secret_value() == DEV_MASTER_KEY_PLACEHOLDER:
            raise MasterKeyError("MASTER_KEY is the development placeholder; refusing to start")
        return self

    @model_validator(mode="after")
    def _validate_dev_mode(self) -> Self:
        if self.auth_dev_mode and self.environment not in (Environment.LOCAL, Environment.TEST):
            raise ValueError(f"AUTH_DEV_MODE cannot be enabled in {self.environment}")
        return self

    @model_validator(mode="after")
    def _validate_backup_target(self) -> Self:
        """A backup target equal to the primary is not a backup."""
        if not self.backup_enabled:
            return self
        missing = [
            name
            for name, value in (
                ("BACKUP_S3_ENDPOINT", self.backup_s3_endpoint),
                ("BACKUP_S3_ACCESS_KEY", self.backup_s3_access_key),
                ("BACKUP_S3_SECRET_KEY", self.backup_s3_secret_key),
                ("BACKUP_S3_BUCKET", self.backup_s3_bucket),
            )
            if value is None
        ]
        if missing:
            raise ValueError(f"BACKUP_ENABLED requires: {', '.join(missing)}")
        if (
            self.backup_s3_endpoint == self.minio_endpoint
            and self.backup_s3_bucket == self.minio_bucket
        ):
            raise ValueError("backup target must differ from the primary MinIO endpoint and bucket")
        return self

    @model_validator(mode="after")
    def _validate_cron_expressions(self) -> Self:
        """Refuse to boot on a schedule that cannot be parsed.

        The scheduler computes its next fire time on every tick, so a bad
        expression would otherwise surface long after startup -- as a background
        task that stopped, with the job silently never running.
        """
        for name, expression in (
            ("BACKUP_CRON", self.backup_cron),
            ("REWRAP_CRON", self.rewrap_cron),
        ):
            try:
                validate_cron(expression)
            except CronError as exc:
                raise ValueError(f"{name} is not a valid cron expression: {exc}") from exc
        return self

    @model_validator(mode="after")
    def _validate_page_sizes(self) -> Self:
        if self.page_size_default > self.page_size_max:
            raise ValueError("PAGE_SIZE_DEFAULT cannot exceed PAGE_SIZE_MAX")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached so validation runs once, at startup."""
    return Settings()  # values come from the environment

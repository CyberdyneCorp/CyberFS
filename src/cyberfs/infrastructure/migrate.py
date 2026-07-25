"""Apply migrations before the API binds its port.

Run as `python -m cyberfs.infrastructure.migrate` from the container
entrypoint. Exits non-zero on failure so the orchestrator never lets a replica
serve traffic against a half-migrated schema.

Replicas starting together serialize on a Postgres advisory lock: exactly one
applies the migrations while the others block, then find nothing to do.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

from cyberfs.infrastructure.db import MIGRATION_LOCK_ID, advisory_lock, create_engine
from cyberfs.infrastructure.logging import configure_logging, get_logger
from cyberfs.infrastructure.settings import get_settings

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def alembic_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return config


async def upgrade_to_head() -> None:
    settings = get_settings()
    engine = create_engine(settings)
    try:
        async with advisory_lock(engine, MIGRATION_LOCK_ID):
            logger.info("migrations_started")
            # Alembic's runner is synchronous and opens its own connection; the
            # lock is held on a separate one for the duration.
            await asyncio.to_thread(command.upgrade, alembic_config(), "head")
            logger.info("migrations_finished")
    finally:
        await engine.dispose()


def main() -> int:
    configure_logging(get_settings().log_level)
    try:
        asyncio.run(upgrade_to_head())
    except Exception as exc:
        logger.error("migrations_failed", error=type(exc).__name__, detail=str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

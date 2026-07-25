"""CyberFS -- backend filesystem service.

Layering (hexagonal / ports & adapters), dependencies point inward only:

    domain/          entities, value objects, invariants, port protocols.
                     No I/O, no framework.
    application/     use cases orchestrating ports; owns the transaction
                     boundary. No FastAPI, no SQLAlchemy.
    adapters/
      inbound/api/   FastAPI routers, schemas, DI, streaming.
      outbound/      SQLAlchemy, MinIO, Redis, CyberdyneAuth, crypto.
    infrastructure/  settings, engine/session, Alembic, logging, metrics.

`tests/unit/test_layering.py` enforces this.
"""

__version__ = "0.1.0"

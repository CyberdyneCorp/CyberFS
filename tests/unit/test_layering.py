"""Guard the hexagonal layering.

The domain and application layers must stay free of frameworks and I/O
libraries so their logic -- sharing rules, inheritance resolution, quota
arithmetic, encryption-state inheritance, cycle detection -- is testable
without Postgres, Redis, MinIO, or FastAPI present. That testability is what
makes the >90% coverage floor honest rather than theatre.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "cyberfs"

# Modules that would drag I/O or a web framework into the pure layers.
FORBIDDEN_IN_PURE_LAYERS = {
    "fastapi",
    "starlette",
    "sqlalchemy",
    "asyncpg",
    "alembic",
    "redis",
    "minio",
    "httpx",
    "uvicorn",
    "prometheus_client",
    # Crypto primitives belong beside the key provider, not in a use case.
    "cryptography",
}

PURE_LAYERS = ("domain", "application")


def _imported_roots(path: Path) -> set[str]:
    """Top-level package name of every import in a module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _modules_in(layer: str) -> list[Path]:
    return sorted((SRC / layer).rglob("*.py"))


def test_pure_layers_import_no_frameworks() -> None:
    violations: list[str] = []
    for layer in PURE_LAYERS:
        for module in _modules_in(layer):
            forbidden = _imported_roots(module) & FORBIDDEN_IN_PURE_LAYERS
            if forbidden:
                rel = module.relative_to(SRC.parent.parent)
                violations.append(f"{rel}: {', '.join(sorted(forbidden))}")
    assert not violations, "framework imports leaked into a pure layer:\n" + "\n".join(violations)


def test_domain_does_not_import_application_or_adapters() -> None:
    """The domain sits at the centre; nothing inward of it exists."""
    violations: list[str] = []
    for module in _modules_in("domain"):
        source = module.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                target = node.module
                if target.startswith(("cyberfs.application", "cyberfs.adapters")):
                    violations.append(f"{module.relative_to(SRC.parent.parent)}: {target}")
    assert not violations, "domain reached outward:\n" + "\n".join(violations)


def test_application_does_not_import_adapters() -> None:
    """Use cases talk to ports, never to concrete adapters."""
    violations: list[str] = []
    for module in _modules_in("application"):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("cyberfs.adapters"):
                    violations.append(f"{module.relative_to(SRC.parent.parent)}: {node.module}")
    assert not violations, "application depended on a concrete adapter:\n" + "\n".join(violations)


def test_layer_directories_exist() -> None:
    for layer in (
        "domain",
        "domain/ports",
        "application",
        "adapters/inbound/api",
        "adapters/outbound",
        "infrastructure",
    ):
        assert (SRC / layer).is_dir(), f"missing layer directory: {layer}"

"""Shared fixtures.

Integration tests need a real CockroachDB. They are skipped unless
PROCUREGUARD_TEST_DB=1 is set, so the default `pytest` run stays hermetic.
"""

from __future__ import annotations

import os

import pytest


def _database_available() -> bool:
    if os.environ.get("PROCUREGUARD_TEST_DB") != "1":
        return False
    try:
        from procureguard.infrastructure.db.session import healthcheck

        return healthcheck().get("status") == "ok"
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not _database_available(),
    reason="set PROCUREGUARD_TEST_DB=1 with a reachable CockroachDB to run integration tests",
)


@pytest.fixture(scope="session")
def seeded_database():
    """Seed a small enterprise once for the whole integration session."""
    from procureguard.seed.runner import seed_database

    report = seed_database(scale="tiny", reset=True)
    assert report.counts["purchase_history"] > 0
    return report


@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient

    from procureguard.api.main import create_app

    with TestClient(create_app()) as client:
        yield client


@pytest.fixture
def buyer_headers() -> dict[str, str]:
    return {"X-Actor-Id": "sam.senior", "X-Actor-Roles": "SENIOR_BUYER"}


@pytest.fixture
def engineer_headers() -> dict[str, str]:
    return {"X-Actor-Id": "priya.engineer", "X-Actor-Roles": "ENGINEER"}


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"X-Actor-Id": "admin", "X-Actor-Roles": "ADMIN"}

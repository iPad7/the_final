"""Shared test fixtures.

The database fixtures need Docker. Locking and concurrency behaviour cannot be
demonstrated on SQLite (ADR23-03/07), so a real PostgreSQL is not optional here.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from testcontainers.community.postgres import PostgresContainer

from fulfill.shared.settings import get_settings

# The same pinned image as docker-compose.yml: a test that passes against a
# different server version than the one developers run proves less than it seems.
POSTGRES_IMAGE = "postgres:18.6"

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def alembic_config() -> Config:
    """Alembic config located by path, so the tests do not depend on the cwd."""
    return Config(str(_REPO_ROOT / "alembic.ini"))


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Start a throwaway PostgreSQL and yield its connection URL."""
    # driver="psycopg": the container default is psycopg2, which this project
    # does not install (ADR23-03 pins psycopg 3).
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session")
def migrated_database(postgres_url: str, alembic_config: Config) -> Iterator[str]:
    """Point Settings at the container, then migrate it to head."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DATABASE_URL", postgres_url)
        # get_settings is lru_cached. Without clearing it, env.py would keep the
        # default URL and migrate the local development database instead of the
        # container the test just started.
        get_settings.cache_clear()
        command.upgrade(alembic_config, "head")
        yield postgres_url
    get_settings.cache_clear()

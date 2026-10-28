"""24-0 gate: migrations actually reach a real PostgreSQL."""

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration


def test_migration_reaches_head(migrated_database: str, alembic_config: Config) -> None:
    # Read head from the script directory rather than hardcoding a revision id,
    # so adding migrations in 24-1 does not break this test.
    expected_head = ScriptDirectory.from_config(alembic_config).get_current_head()

    engine = create_engine(migrated_database)
    try:
        with engine.connect() as connection:
            applied: str = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            server_version: str = connection.execute(text("SHOW server_version")).scalar_one()
    finally:
        engine.dispose()

    assert applied == expected_head
    # Confirms the server behind the test is the pinned PostgreSQL 18, not a stand-in.
    assert server_version.startswith("18.")

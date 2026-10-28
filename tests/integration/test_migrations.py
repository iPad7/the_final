"""마이그레이션이 실제 PostgreSQL에 실제로 도달하는지 확인한다(24-0 관문)."""

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration


def test_migration_reaches_head(migrated_database: str, alembic_config: Config) -> None:
    # 리비전 id를 하드코딩하지 않고 스크립트 디렉터리에서 head를 읽는다. 이후
    # 마이그레이션이 늘어도 이 테스트가 깨지지 않는다.
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
    # 테스트 뒤의 서버가 대역이 아니라 고정한 PostgreSQL 18임을 확인한다.
    assert server_version.startswith("18.")

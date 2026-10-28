"""Alembic 실행 환경.

DB URL은 `alembic.ini`가 아니라 `Settings`를 통해 해석한다. 그래서 로컬 compose
DB, 통합 테스트가 쓰는 임시 컨테이너, CI가 모두 같은 방식으로 읽는다(환경변수
DATABASE_URL이 우선한다).
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from fulfill import metadata as fulfill_metadata
from fulfill.shared.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 스키마 조합 지점에서 가져온다. 우연히 import된 것만이 아니라 모든 모듈의
# 테이블과 비교하도록 하기 위한 것이다.
target_metadata = fulfill_metadata.target_metadata


def run_migrations_offline() -> None:
    """DB에 접속하지 않고 마이그레이션을 SQL로 출력한다."""
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """실제 커넥션에 대해 마이그레이션을 실행한다.

    NullPool을 쓰는 이유: 마이그레이션 실행은 짧게 끝나므로, 풀에 남은 커넥션은
    프로세스가 끝난 뒤에도 DB를 붙잡고 있을 뿐이다.
    """
    connectable = create_engine(get_settings().database_url, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

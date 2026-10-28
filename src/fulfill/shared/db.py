"""Declarative base, 엔진, Unit of Work.

모듈은 하나의 PostgreSQL 위에 놓인 논리적 경계이므로(ADR23-02/03) 모든 모듈이
이 하나의 `Base`에 테이블을 올린다. metadata가 하나면 스키마 전체의 Alembic
히스토리도 한 줄로 유지된다.
"""

from collections.abc import Generator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from fulfill.shared.settings import get_settings

# 이것이 없으면 PK·FK 이름을 PostgreSQL이 임의로 정하고, 나중에 그 제약을
# 변경·삭제하는 마이그레이션이 서버가 고른 이름에 의존하게 된다. UNIQUE와 CHECK는
# 각 테이블에서 명시적으로 이름을 붙이므로 여기 넣지 않고 그 이름을 유지한다.
NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "ix": "ix_%(table_name)s_%(column_0_name)s",
}


class Base(DeclarativeBase):
    """공용 declarative base. 각 모듈의 ORM 모델이 이를 상속한다."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


@lru_cache
def get_engine() -> Engine:
    """프로세스 단위 엔진 (ADR23-04: 동기 Session).

    `get_settings`와 마찬가지로 캐시되므로, DATABASE_URL을 바꾸는 테스트는 이
    캐시도 비워야 한다. 비우지 않으면 이전 URL의 DB를 계속 바라본다.
    """
    return create_engine(get_settings().database_url)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def unit_of_work() -> Generator[Session]:
    """블록 하나에 트랜잭션 하나. 그 블록이 건드리는 모든 모듈이 공유한다.

    모듈을 넘는 작업을 모듈별로 커밋하지 않고 여기서 조정한다(ADR23 §3). 수요
    구간과 그에 발급된 FIFO 순번이 서로 다른 커밋에 들어가는 일을 막기 위한
    것이다. 예외가 나면 블록 전체를 롤백한다.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_db_caches() -> None:
    """캐시된 설정·엔진·세션 팩토리를 모두 버린다.

    테스트는 DATABASE_URL을 임시 컨테이너로 돌린다. 이 함수가 없으면 이전 URL로
    만든 엔진을 계속 쓰게 된다.
    """
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

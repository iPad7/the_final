"""공용 테스트 픽스처.

DB 픽스처는 Docker가 필요하다. 잠금과 동시성 동작은 SQLite로 보일 수 없으므로
(ADR23-03/07) 실제 PostgreSQL은 선택 사항이 아니다.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from testcontainers.community.postgres import PostgresContainer

from fulfill.modules.requests.domain import IntakeCommand, Recipient, RequestedLine
from fulfill.shared.db import reset_db_caches, unit_of_work
from tests.seed_data import (
    SELLER_A,
    SELLER_B,
    SKU_P,
    SKU_Q,
    TABLES_IN_DELETE_ORDER,
)

# docker-compose.yml과 같은 태그로 고정한다. 개발자가 쓰는 것과 다른 서버 버전에서
# 통과한 테스트는 보이는 것보다 증명하는 바가 적다.
POSTGRES_IMAGE = "postgres:18.6"

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def alembic_config() -> Config:
    """경로로 찾는 Alembic 설정. 테스트가 현재 작업 디렉터리에 의존하지 않게 한다."""
    return Config(str(_REPO_ROOT / "alembic.ini"))


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """임시 PostgreSQL을 띄우고 접속 URL을 넘긴다."""
    # driver="psycopg": 컨테이너 기본값은 psycopg2인데 이 프로젝트는 설치하지
    # 않는다(ADR23-03이 psycopg 3을 고정).
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session")
def migrated_database(postgres_url: str, alembic_config: Config) -> Iterator[str]:
    """Settings를 컨테이너로 돌린 뒤 스키마를 head까지 올린다."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DATABASE_URL", postgres_url)
        # 설정·엔진·세션 팩토리가 모두 캐시된다. 비우지 않으면 env.py와 UoW가
        # 기본 URL을 그대로 써서, 방금 띄운 컨테이너가 아니라 로컬 개발 DB를
        # 건드릴 수 있다.
        reset_db_caches()
        command.upgrade(alembic_config, "head")
        yield postgres_url
    reset_db_caches()


@pytest.fixture
def seeded_database(migrated_database: str) -> Iterator[str]:
    """비어 있는 스키마에 판매사 2곳과 SKU 2종을 넣는다."""
    with unit_of_work() as session:
        for table in TABLES_IN_DELETE_ORDER:
            session.execute(text(f"DELETE FROM {table}"))
        for seller_code in (SELLER_A, SELLER_B):
            seller_id = uuid.uuid4()
            session.execute(
                text("INSERT INTO seller (id, code, name) VALUES (:id, :code, :code)"),
                {"id": seller_id, "code": seller_code},
            )
            for sku_code in (SKU_P, SKU_Q):
                session.execute(
                    text(
                        "INSERT INTO sku (id, seller_id, code, name)"
                        " VALUES (:id, :seller_id, :code, :code)"
                    ),
                    {"id": uuid.uuid4(), "seller_id": seller_id, "code": sku_code},
                )
    yield migrated_database


@pytest.fixture
def base_command() -> IntakeCommand:
    """유효한 접수 명령 하나. 각 테스트는 dataclasses.replace로 변형한다.

    테스트마다 전체를 다시 쓰면 무엇이 다른지 드러나지 않는다.
    """
    return IntakeCommand(
        seller_code=SELLER_A,
        external_request_id="EXT-1",
        lines=(RequestedLine(SKU_P, 5), RequestedLine(SKU_Q, 2)),
        recipient=Recipient(name="홍길동", address="서울시 어딘가 101", phone="010-0000-0000"),
        initial_due_at=datetime(2026, 11, 5, 0, tzinfo=UTC),
        desired_ship_date=None,
        occurred_at=datetime(2026, 10, 28, 1, tzinfo=UTC),
    )

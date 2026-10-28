"""기준선

일부러 비워 둔다. 리비전이 하나도 없으면 `alembic upgrade head`가 아무 일도 하지
않고 alembic_version 테이블조차 생기지 않아, 마이그레이션이 DB에 도달한다는 것을
보일 방법이 없다. 이 리비전을 적용하면 실제 PostgreSQL에 대해 그 경로가 동작함이
증명된다. 실제 테이블은 다음 증분에서 들어온다.

Revision ID: 6f30c20188da
Revises:
Create Date: 2026-10-27 23:46:13.398979

"""

from collections.abc import Sequence

# Alembic이 사용하는 리비전 식별자.
revision: str = "6f30c20188da"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """스키마 변경 없음. 모듈 docstring 참고."""


def downgrade() -> None:
    """스키마 변경 없음. 모듈 docstring 참고."""

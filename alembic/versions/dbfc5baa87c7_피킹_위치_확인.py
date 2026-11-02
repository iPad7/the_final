"""피킹 위치 확인

Revision ID: dbfc5baa87c7
Revises: 0afe6e718662
Create Date: 2026-11-02 19:31:34.711874

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic이 사용하는 리비전 식별자.
revision: str = "dbfc5baa87c7"
down_revision: str | Sequence[str] | None = "0afe6e718662"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 자동 생성본의 DDL을 그대로 썼다. 새 테이블 하나뿐이고 CHECK 2개와 FK 2개가 모두
# 들어 있었으며, 적용 후 pg_constraint로 대조했다.
#
# 이 리비전은 stock_position을 참조하는 FK를 만들지만 그 테이블을 바꾸지 않는다.
# 기존 테이블의 CHECK를 건드리는 변경이 아니므로 24-3에서 세 번 빠진 경로가
# 아니다(CLAUDE.md 8장).

# 모델과 같은 식이어야 한다. 두 곳에 같은 문자열이 존재하지만 Alembic이 모델을
# 읽지 않으므로 피할 수 없고, 어긋나면 스키마 비교 테스트가 잡는다.
CHANGES_LOCATION = "previous_location_code IS NULL OR previous_location_code <> location_code"


def upgrade() -> None:
    """스키마 상향 적용."""
    op.create_table(
        "location_confirmation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("receipt_id", sa.Uuid(), nullable=False),
        sa.Column("stock_position_id", sa.Uuid(), nullable=False),
        sa.Column("previous_location_code", sa.String(length=100), nullable=True),
        sa.Column("location_code", sa.String(length=100), nullable=False),
        sa.Column("confirmed_by", sa.String(length=100), nullable=False),
        sa.Column("environment_class", sa.String(length=20), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "environment_class IN ('SIMULATION')",
            name="ck_location_confirmation_environment_class",
        ),
        # 확인 전과 후가 같은 행은 확인이 아니다. 조정 계층이 그 입력을 변화
        # 없음으로 판정하므로 정상 경로에서는 도달하지 않는다.
        sa.CheckConstraint(CHANGES_LOCATION, name="ck_location_confirmation_changes_location"),
        sa.ForeignKeyConstraint(
            ["receipt_id"], ["receipt.id"], name=op.f("fk_location_confirmation_receipt_id")
        ),
        sa.ForeignKeyConstraint(
            ["stock_position_id"],
            ["stock_position.id"],
            name=op.f("fk_location_confirmation_stock_position_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_location_confirmation")),
    )


def downgrade() -> None:
    """스키마 하향 복구."""
    op.drop_table("location_confirmation")

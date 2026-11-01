"""allocation tables

Revision ID: b77848b96971
Revises: c09adc7b3161
Create Date: 2026-11-01 14:53:19.229024

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic이 사용하는 리비전 식별자.
revision: str = "b77848b96971"
down_revision: str | Sequence[str] | None = "c09adc7b3161"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 자동 생성본을 손으로 고쳤다. autogenerate는 **기존 테이블에 추가되는 CHECK
# 제약을 감지하지 않는다.** 그래서 stock_position의
# ck_stock_allocated_within_quantity가 생성본에 없었고, 그대로 적용했다면 이중
# 배정을 막는 장치가 모델에만 있고 DB에는 없는 상태가 된다.
#
# 24-2에서는 같은 이유로 제약 생성 순서가 두 번 틀렸다. 모델이 맞다는 것과
# 마이그레이션이 맞다는 것은 다른 사실이다(CLAUDE.md 8장).

# 모델과 같은 식이어야 한다. 두 곳에 같은 문자열이 존재하지만 Alembic이 모델을
# 읽지 않으므로 피할 수 없고, 어긋나면 스키마 비교 테스트가 잡는다.
ALLOCATED_WITHIN_QUANTITY = "allocated_quantity >= 0 AND allocated_quantity <= quantity"


def upgrade() -> None:
    """스키마 상향 적용."""
    op.create_table(
        "allocation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("demand_segment_id", sa.Uuid(), nullable=False),
        sa.Column("stock_position_id", sa.Uuid(), nullable=False),
        sa.Column("sku_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column(
            "allocated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("environment_class", sa.String(length=20), nullable=False),
        sa.CheckConstraint(
            "environment_class IN ('SIMULATION')", name="ck_allocation_environment_class"
        ),
        sa.CheckConstraint("quantity > 0", name="ck_allocation_quantity_positive"),
        sa.ForeignKeyConstraint(
            ["demand_segment_id"],
            ["demand_segment.id"],
            name=op.f("fk_allocation_demand_segment_id"),
        ),
        sa.ForeignKeyConstraint(["sku_id"], ["sku.id"], name=op.f("fk_allocation_sku_id")),
        sa.ForeignKeyConstraint(
            ["stock_position_id"],
            ["stock_position.id"],
            name=op.f("fk_allocation_stock_position_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_allocation")),
    )

    # server_default가 있어야 이미 있는 버킷 행이 0으로 채워진다. 없으면 NOT NULL
    # 컬럼 추가가 기존 행에서 실패한다.
    op.add_column(
        "stock_position",
        sa.Column("allocated_quantity", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )

    # 자동 생성본에 없던 제약. 컬럼이 생긴 뒤에 걸어야 하므로 순서가 중요하다.
    op.create_check_constraint(
        "ck_stock_allocated_within_quantity", "stock_position", ALLOCATED_WITHIN_QUANTITY
    )


def downgrade() -> None:
    """스키마 하향 복구."""
    # upgrade의 역순이다. PostgreSQL은 컬럼을 지울 때 그 컬럼에 의존하는 제약도
    # 함께 지우지만, 명시해 두면 무엇이 사라지는지 읽는 사람이 알 수 있다.
    op.drop_constraint("ck_stock_allocated_within_quantity", "stock_position", type_="check")
    op.drop_column("stock_position", "allocated_quantity")
    op.drop_table("allocation")

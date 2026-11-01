"""demand revision and allocation release

Revision ID: ce63971b651a
Revises: b77848b96971
Create Date: 2026-11-01 16:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic이 사용하는 리비전 식별자.
revision: str = "ce63971b651a"
down_revision: str | Sequence[str] | None = "b77848b96971"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 자동 생성본을 손으로 고쳤다. autogenerate가 **CHECK 제약 변경을 두 개 놓쳤다.**
#
# 1. allocation.released_quantity의 상한 제약 — 기존 테이블에 추가되는 CHECK는
#    감지되지 않는다(24-3 단계 1에서도 같은 일이 있었다).
# 2. demand_segment의 수량 제약을 >= 0으로 바꾸는 것 — 식이 바뀌어도 감지되지
#    않는다. 빠뜨리면 DB에는 > 0이 남아, 감량으로 구간이 0이 되는 순간에만
#    터지는 잠복 버그가 된다.

RELEASED_WITHIN_QUANTITY = "released_quantity >= 0 AND released_quantity <= quantity"


def upgrade() -> None:
    """스키마 상향 적용."""
    op.create_table(
        "request_revision",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("line_id", sa.Uuid(), nullable=False),
        sa.Column("previous_quantity", sa.Integer(), nullable=False),
        sa.Column("new_quantity", sa.Integer(), nullable=False),
        sa.Column("revised_by", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("environment_class", sa.String(length=20), nullable=False),
        sa.CheckConstraint(
            "environment_class IN ('SIMULATION')", name="ck_revision_environment_class"
        ),
        sa.CheckConstraint("new_quantity > 0", name="ck_revision_new_quantity_positive"),
        sa.CheckConstraint(
            "previous_quantity <> new_quantity", name="ck_revision_quantity_changed"
        ),
        sa.CheckConstraint("previous_quantity > 0", name="ck_revision_previous_quantity_positive"),
        sa.ForeignKeyConstraint(
            ["line_id"], ["request_line.id"], name=op.f("fk_request_revision_line_id")
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["fulfillment_request.id"],
            name=op.f("fk_request_revision_request_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_request_revision")),
    )

    # 컬럼을 먼저 만들고 제약을 걸어야 한다. 순서가 뒤집히면 없는 컬럼을
    # 참조하게 된다.
    op.add_column(
        "allocation",
        sa.Column("released_quantity", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    # 자동 생성본에 없던 제약.
    op.create_check_constraint(
        "ck_allocation_released_within_quantity", "allocation", RELEASED_WITHIN_QUANTITY
    )

    op.create_table(
        "allocation_release",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("allocation_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=30), nullable=False),
        sa.Column(
            "released_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("environment_class", sa.String(length=20), nullable=False),
        sa.CheckConstraint(
            "environment_class IN ('SIMULATION')", name="ck_release_environment_class"
        ),
        sa.CheckConstraint("reason IN ('DEMAND_DECREASE')", name="ck_release_reason"),
        sa.CheckConstraint("quantity > 0", name="ck_release_quantity_positive"),
        sa.ForeignKeyConstraint(
            ["allocation_id"],
            ["allocation.id"],
            name=op.f("fk_allocation_release_allocation_id"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_allocation_release")),
    )

    # 자동 생성본에 없던 제약 교체. 감량이 증량 구간을 전부 지우면 구간 수량이
    # 0이 되는데, 행을 삭제할 수는 없다 — allocation이 참조하고 있고 지우면 배정
    # 이력을 잃는다(AC-36).
    op.drop_constraint("ck_segment_quantity_positive", "demand_segment", type_="check")
    op.create_check_constraint(
        "ck_segment_quantity_not_negative", "demand_segment", "quantity >= 0"
    )


def downgrade() -> None:
    """스키마 하향 복구."""
    # 주의: 아래 제약 복구는 수량 0인 수요 구간이 하나라도 있으면 실패한다.
    # 감량이 일어난 뒤 이 리비전을 되돌리려면 그 행들을 먼저 정리해야 한다.
    # 더 느슨한 제약으로 올라간 마이그레이션을 되돌리는 일에 공통된 성질이며,
    # 숨기지 않고 남겨 둔다.
    op.drop_constraint("ck_segment_quantity_not_negative", "demand_segment", type_="check")
    op.create_check_constraint("ck_segment_quantity_positive", "demand_segment", "quantity > 0")

    op.drop_table("allocation_release")
    op.drop_constraint("ck_allocation_released_within_quantity", "allocation", type_="check")
    op.drop_column("allocation", "released_quantity")
    op.drop_table("request_revision")

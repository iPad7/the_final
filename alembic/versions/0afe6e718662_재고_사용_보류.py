"""재고 사용 보류

Revision ID: 0afe6e718662
Revises: ce63971b651a
Create Date: 2026-11-01 20:36:15.481552

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic이 사용하는 리비전 식별자.
revision: str = "0afe6e718662"
down_revision: str | Sequence[str] | None = "ce63971b651a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 자동 생성본의 DDL을 그대로 썼다. 이번에는 CHECK 제약 셋과 부분 유니크 인덱스가
# 모두 생성본에 들어 있었고, 적용 후 pg_constraint·pg_index로 대조했다.
#
# 24-3에서 세 번 빠진 것은 **기존 테이블에 추가되는** CHECK였다. 새 테이블은
# create_table이 __table_args__를 그대로 옮기므로 그 경로가 아니다. 다음에 기존
# 테이블의 CHECK를 건드리면 다시 직접 확인해야 한다(CLAUDE.md 8장).

# 모델과 같은 식이어야 한다. 두 곳에 같은 문자열이 존재하지만 Alembic이 모델을
# 읽지 않으므로 피할 수 없고, 어긋나면 스키마 비교 테스트가 잡는다.
LIFT_FIELDS_TOGETHER = (
    "(lifted_at IS NULL AND lifted_by IS NULL AND lifted_recorded_at IS NULL)"
    " OR (lifted_at IS NOT NULL AND lifted_by IS NOT NULL"
    " AND lifted_recorded_at IS NOT NULL)"
)


def upgrade() -> None:
    """스키마 상향 적용."""
    op.create_table(
        "inventory_hold",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("receipt_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(length=30), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("held_by", sa.String(length=100), nullable=False),
        sa.Column("environment_class", sa.String(length=20), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lifted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lifted_recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lifted_by", sa.String(length=100), nullable=True),
        sa.Column("lifted_note", sa.Text(), nullable=True),
        sa.CheckConstraint("environment_class IN ('SIMULATION')", name="ck_hold_environment_class"),
        sa.CheckConstraint(
            "reason IN ('COUNT_DISCREPANCY', 'QUALITY_SUSPECTED', 'OTHER')",
            name="ck_hold_reason",
        ),
        sa.CheckConstraint(LIFT_FIELDS_TOGETHER, name="ck_hold_lift_fields_together"),
        sa.ForeignKeyConstraint(
            ["receipt_id"], ["receipt.id"], name=op.f("fk_inventory_hold_receipt_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_hold")),
    )
    # 부분 유니크 인덱스다. WHERE 절이 있어야 "활성 보류는 하나"가 되고, 없으면
    # 입고묶음당 보류를 평생 한 번만 걸 수 있게 되어 재보류가 불가능해진다.
    op.create_index(
        "uq_hold_active_per_receipt",
        "inventory_hold",
        ["receipt_id"],
        unique=True,
        postgresql_where=sa.text("lifted_at IS NULL"),
    )


def downgrade() -> None:
    """스키마 하향 복구."""
    # 인덱스를 먼저 지운다. 테이블을 지우면 인덱스도 함께 사라지지만, 순서를
    # 명시해 두면 이 리비전만 따로 되돌릴 때 의도가 드러난다.
    op.drop_index(
        "uq_hold_active_per_receipt",
        table_name="inventory_hold",
        postgresql_where=sa.text("lifted_at IS NULL"),
    )
    op.drop_table("inventory_hold")

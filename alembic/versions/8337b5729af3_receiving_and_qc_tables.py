"""입고 예정·실도착·검수 판정·재고 버킷 테이블 추가

Revision ID: 8337b5729af3
Revises: d121330aa199
Create Date: 2026-10-30 16:44:47.716167

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic이 사용하는 리비전 식별자.
revision: str = "8337b5729af3"
down_revision: str | Sequence[str] | None = "d121330aa199"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """스키마 상향 적용."""
    # ### Alembic이 자동 생성한 명령 — 검토 후 사용 ###
    # autogenerate는 이 줄을 맨 끝에 뒀는데 그러면 적용이 실패한다. receipt의
    # 복합 FK가 sku (seller_id, id)를 참조하므로 그 유니크 제약이 먼저 있어야
    # 한다. autogenerate는 객체별로 나열할 뿐 새 테이블이 기존 테이블의 새 제약에
    # 의존한다는 관계를 알지 못한다.
    op.create_unique_constraint("uq_sku_seller_scoped_id", "sku", ["seller_id", "id"])
    op.create_table(
        "inbound_advice",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sku_id", sa.Uuid(), nullable=False),
        sa.Column("expected_quantity", sa.Integer(), nullable=False),
        sa.Column("advised_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("expected_quantity > 0", name="ck_advice_quantity_positive"),
        sa.ForeignKeyConstraint(["sku_id"], ["sku.id"], name=op.f("fk_inbound_advice_sku_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inbound_advice")),
    )
    op.create_table(
        "receipt",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("advice_id", sa.Uuid(), nullable=True),
        sa.Column("seller_id", sa.Uuid(), nullable=True),
        sa.Column("sku_id", sa.Uuid(), nullable=True),
        sa.Column("ownership_status", sa.String(length=20), nullable=False),
        sa.Column("arrived_quantity", sa.Integer(), nullable=False),
        sa.Column("confirmed_by", sa.String(length=100), nullable=False),
        sa.Column("environment_class", sa.String(length=20), nullable=False),
        sa.Column("arrived_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "environment_class IN ('SIMULATION')", name="ck_receipt_environment_class"
        ),
        sa.CheckConstraint(
            "ownership_status <> 'CONFIRMED' OR (seller_id IS NOT NULL AND sku_id IS NOT NULL)",
            name="ck_receipt_confirmed_attribution",
        ),
        sa.CheckConstraint(
            "ownership_status IN ('CONFIRMED', 'UNKNOWN')", name="ck_receipt_ownership_status"
        ),
        sa.CheckConstraint("arrived_quantity > 0", name="ck_receipt_quantity_positive"),
        sa.CheckConstraint(
            "sku_id IS NULL OR seller_id IS NOT NULL", name="ck_receipt_sku_implies_seller"
        ),
        sa.ForeignKeyConstraint(
            ["advice_id"], ["inbound_advice.id"], name=op.f("fk_receipt_advice_id")
        ),
        sa.ForeignKeyConstraint(
            ["seller_id", "sku_id"], ["sku.seller_id", "sku.id"], name="fk_receipt_seller_sku"
        ),
        sa.ForeignKeyConstraint(["seller_id"], ["seller.id"], name=op.f("fk_receipt_seller_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_receipt")),
    )
    op.create_table(
        "qc_decision",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("receipt_id", sa.Uuid(), nullable=False),
        sa.Column("result", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("decided_by", sa.String(length=100), nullable=False),
        sa.Column("environment_class", sa.String(length=20), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("environment_class IN ('SIMULATION')", name="ck_qc_environment_class"),
        sa.CheckConstraint("result IN ('PASS', 'FAIL')", name="ck_qc_result"),
        sa.CheckConstraint("quantity > 0", name="ck_qc_quantity_positive"),
        sa.ForeignKeyConstraint(
            ["receipt_id"], ["receipt.id"], name=op.f("fk_qc_decision_receipt_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_qc_decision")),
    )
    op.create_table(
        "stock_position",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sku_id", sa.Uuid(), nullable=False),
        sa.Column("receipt_id", sa.Uuid(), nullable=False),
        sa.Column("qc_state", sa.String(length=20), nullable=False),
        sa.Column("location_code", sa.String(length=100), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("qc_state IN ('PENDING', 'PASSED', 'FAILED')", name="ck_stock_qc_state"),
        sa.CheckConstraint("quantity >= 0", name="ck_stock_quantity_not_negative"),
        sa.ForeignKeyConstraint(
            ["receipt_id"], ["receipt.id"], name=op.f("fk_stock_position_receipt_id")
        ),
        sa.ForeignKeyConstraint(["sku_id"], ["sku.id"], name=op.f("fk_stock_position_sku_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_position")),
        sa.UniqueConstraint(
            "sku_id",
            "receipt_id",
            "qc_state",
            "location_code",
            name="uq_stock_bucket",
            postgresql_nulls_not_distinct=True,
        ),
    )
    # ### Alembic 명령 끝 ###


def downgrade() -> None:
    """스키마 하향 복구."""
    # ### Alembic이 자동 생성한 명령 — 검토 후 사용 ###
    op.drop_table("stock_position")
    op.drop_table("qc_decision")
    op.drop_table("receipt")
    op.drop_table("inbound_advice")
    # upgrade와 거울이 되도록 맨 뒤에 둔다. receipt의 FK가 이 제약에 의존하므로
    # 테이블보다 먼저 지우면 PostgreSQL이 거부한다.
    op.drop_constraint("uq_sku_seller_scoped_id", "sku", type_="unique")
    # ### Alembic 명령 끝 ###

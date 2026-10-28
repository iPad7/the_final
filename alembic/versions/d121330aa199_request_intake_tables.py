"""출고 요청 접수 테이블 생성

Revision ID: d121330aa199
Revises: 6f30c20188da
Create Date: 2026-10-28 18:24:58.264391

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic이 사용하는 리비전 식별자.
revision: str = "d121330aa199"
down_revision: str | Sequence[str] | None = "6f30c20188da"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """스키마 상향 적용."""
    # ### Alembic이 자동 생성한 명령 — 검토 후 사용 ###
    op.create_table(
        "seller",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seller")),
        sa.UniqueConstraint("code", name="uq_seller_code"),
    )
    op.create_table(
        "fulfillment_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("seller_id", sa.Uuid(), nullable=False),
        sa.Column("external_request_id", sa.String(length=100), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("normalization_version", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("environment_class", sa.String(length=20), nullable=False),
        sa.Column("recipient_name", sa.String(length=100), nullable=False),
        sa.Column("recipient_address", sa.Text(), nullable=False),
        sa.Column("recipient_phone", sa.String(length=40), nullable=False),
        sa.Column("desired_ship_date", sa.Date(), nullable=True),
        sa.Column("initial_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "environment_class IN ('SIMULATION')", name="ck_request_environment_class"
        ),
        sa.CheckConstraint("status IN ('ACCEPTED')", name="ck_request_status"),
        sa.ForeignKeyConstraint(
            ["seller_id"], ["seller.id"], name=op.f("fk_fulfillment_request_seller_id")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fulfillment_request")),
        sa.UniqueConstraint(
            "seller_id", "external_request_id", name="uq_request_seller_external_id"
        ),
    )
    op.create_table(
        "sku",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("seller_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["seller_id"], ["seller.id"], name=op.f("fk_sku_seller_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sku")),
        sa.UniqueConstraint("seller_id", "code", name="uq_sku_seller_code"),
    )
    op.create_table(
        "request_line",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("sku_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_line_quantity_positive"),
        sa.ForeignKeyConstraint(
            ["request_id"], ["fulfillment_request.id"], name=op.f("fk_request_line_request_id")
        ),
        sa.ForeignKeyConstraint(["sku_id"], ["sku.id"], name=op.f("fk_request_line_sku_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_request_line")),
        sa.UniqueConstraint("request_id", "sku_id", name="uq_line_request_sku"),
    )
    op.create_table(
        "sku_demand_guard",
        sa.Column("sku_id", sa.Uuid(), nullable=False),
        sa.Column("next_demand_seq", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.ForeignKeyConstraint(["sku_id"], ["sku.id"], name=op.f("fk_sku_demand_guard_sku_id")),
        sa.PrimaryKeyConstraint("sku_id", name=op.f("pk_sku_demand_guard")),
    )
    op.create_table(
        "demand_segment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("line_id", sa.Uuid(), nullable=False),
        sa.Column("sku_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('ORIGINAL', 'INCREASE')", name="ck_segment_kind"),
        sa.CheckConstraint("quantity > 0", name="ck_segment_quantity_positive"),
        sa.ForeignKeyConstraint(
            ["line_id"], ["request_line.id"], name=op.f("fk_demand_segment_line_id")
        ),
        sa.ForeignKeyConstraint(["sku_id"], ["sku.id"], name=op.f("fk_demand_segment_sku_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_demand_segment")),
        sa.UniqueConstraint("sku_id", "seq", name="uq_segment_sku_seq"),
    )
    # ### Alembic 명령 끝 ###


def downgrade() -> None:
    """스키마 하향 복구."""
    # ### Alembic이 자동 생성한 명령 — 검토 후 사용 ###
    op.drop_table("demand_segment")
    op.drop_table("sku_demand_guard")
    op.drop_table("request_line")
    op.drop_table("sku")
    op.drop_table("fulfillment_request")
    op.drop_table("seller")
    # ### Alembic 명령 끝 ###

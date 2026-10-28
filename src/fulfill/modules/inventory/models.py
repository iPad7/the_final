"""품목 마스터 ORM 모델 (BC-03)."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from fulfill.shared.db import Base


class Sku(Base):
    """소유 판매사에 종속된 관리 품목.

    수량을 일부러 갖지 않는다. 재고는 `stock_position`에 두어, 실도착·QC 합격·
    배정 가능·피킹 가능이라는 서로 다른 사실들이 숫자 하나로 뭉개지지 않게 한다.
    """

    __tablename__ = "sku"
    __table_args__ = (
        # 두 판매사의 같은 품목 코드는 서로 다른 물건이며, 재고를 절대 섞어
        # 쓰지 않는다.
        UniqueConstraint("seller_id", "code", name="uq_sku_seller_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    seller_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("seller.id"))
    code: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

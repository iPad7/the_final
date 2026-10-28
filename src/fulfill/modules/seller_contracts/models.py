"""판매사 ORM 모델 (BC-01).

판매사는 이 시스템의 가장 바깥 업무 경계다. 재고 소유권, 외부 요청 ID가 유일한
범위, 계약 조건이 모두 판매사 단위로 갈린다.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from fulfill.shared.db import Base


class Seller(Base):
    __tablename__ = "seller"
    __table_args__ = (UniqueConstraint("code", name="uq_seller_code"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # SELLER-A 같은 운영자용 식별자. 운영사가 발급하므로 전역 유일하다. 판매사만
    # 통제하는 SKU 코드와 다른 점이다.
    code: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

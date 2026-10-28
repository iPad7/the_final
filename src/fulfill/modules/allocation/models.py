"""FIFO 큐 guard ORM 모델 (BC-04)."""

import uuid

from sqlalchemy import BigInteger, ForeignKey, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from fulfill.shared.db import Base


class SkuDemandGuard(Base):
    """SKU당 한 행. FIFO 순번 발급을 직렬화하는 잠금 지점이다.

    판매사+SKU 큐(19 §3)의 물리적 형태다. PK가 SKU 하나인 이유는 SKU가 이미
    정확히 한 판매사에 속하기 때문이다. seller_id를 여기 또 두면 두 값이 어긋날
    여지만 생긴다.

    PostgreSQL sequence로는 이 일을 할 수 없다. nextval()은 트랜잭션에 묶이지
    않아 롤백해도 되돌지 않고 결번이 생기며, 더 중요하게는 커밋 순서가 아니라
    insert 순서로 번호를 준다. FOR UPDATE로 잠근 행은 커밋 순서대로 순번을
    발급하고 트랜잭션과 함께 카운터도 되돌린다.
    """

    __tablename__ = "sku_demand_guard"

    sku_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sku.id"), primary_key=True)
    next_demand_seq: Mapped[int] = mapped_column(BigInteger, default=1, server_default=text("1"))

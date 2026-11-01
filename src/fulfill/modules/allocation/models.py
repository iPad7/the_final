"""FIFO 큐 guard·배정 ORM 모델 (BC-04)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from fulfill.shared.db import Base

# requests·inventory가 각자 같은 상수를 들고 있다. 모듈 경계가 서로의 models를
# 보지 못하게 하므로 피할 수 없고, 세 값이 어긋나면 깨지는 테스트를 둔다.
ENVIRONMENT_SIMULATION = "SIMULATION"


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


class Allocation(Base):
    """수요 구간 하나에 재고 버킷 하나에서 독점 배정한 수량.

    19 §3이 Allocation을 "수요 구간에 독점 배정된 물량·출처·정당 해제/사용 근거"로
    정의한다. `stock_position_id`가 그 "출처"다. 수요 구간만 가리키고 수량만 적으면
    어느 실물인지 잃고, 24-4가 AC-09의 피킹 위치를 확인할 대상을 특정할 수 없다.

    한 수요 구간에 여러 행이 생기는 것이 정상이다. 분할 입고로 적격 수량이 여러
    버킷에 흩어져 있으면 수요 5개가 버킷 3+2로 채워진다. 같은 (구간, 버킷) 쌍이
    다시 나타날 수도 있다 — 1차 배정 뒤 그 버킷에 재고가 늘면 2차 배정이 같은 쌍에
    추가로 붙는다. 그래서 그 쌍에 유니크 제약을 걸지 않는다. 행을 합치면 각
    배정의 시각이 사라져 AC-36의 이력 보존을 깬다.

    이중 배정을 막는 것은 이 테이블이 아니다. `stock_position.allocated_quantity`의
    CHECK가 막는다. 여기 쌓이는 것은 "무엇을 왜 배정했는가"의 사실이고, 수량
    한도는 재고 쪽 제약이 지킨다.
    """

    __tablename__ = "allocation"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_allocation_quantity_positive"),
        CheckConstraint(
            f"environment_class IN ('{ENVIRONMENT_SIMULATION}')",
            name="ck_allocation_environment_class",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    demand_segment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("demand_segment.id"))
    stock_position_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stock_position.id"))

    # 두 참조를 타고 가면 알 수 있는 값을 일부러 중복해 둔다. 성능이 아니라 의존성
    # 때문이다. 배정 실행은 "이 SKU의 구간별 배정 합계"를 읽어야 하는데,
    # demand_segment를 조인하면 BC-04가 BC-02의 ORM을 직접 보게 되어 모듈 경계가
    # 깨진다(ADR23 §3). 이 컬럼이 그 조인을 없앤다.
    sku_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sku.id"))

    quantity: Mapped[int] = mapped_column(Integer)

    # 시각이 하나뿐인 것이 입고·검수와 다른 점이다. 실도착은 창고에서 일어난 뒤
    # 입력되므로 발생과 기록이 갈렸지만, 배정은 이 쓰기 자체가 사건이다. 두
    # 컬럼으로 나누면 없는 구분을 꾸미게 된다.
    #
    # FIFO 순서를 이 값으로 판정하지 않는다. 한 번의 배정 실행에서 만들어진 행은
    # 모두 같은 트랜잭션 시각을 갖고, 순서의 근거는 demand_segment.seq다
    # (20 §4: "단순 DB 생성시각으로 FIFO를 대체하지 않는다").
    allocated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    environment_class: Mapped[str] = mapped_column(String(20), default=ENVIRONMENT_SIMULATION)

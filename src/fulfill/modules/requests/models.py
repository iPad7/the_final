"""출고 요청 ORM 모델 (BC-02)."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from fulfill.shared.db import Base

REQUEST_STATUS_ACCEPTED = "ACCEPTED"
ENVIRONMENT_SIMULATION = "SIMULATION"

DEMAND_KIND_ORIGINAL = "ORIGINAL"
DEMAND_KIND_INCREASE = "INCREASE"


class FulfillmentRequest(Base):
    """판매사로부터 수신한 출고 지시.

    `status`에는 접수 상태만 둔다. 배정·피킹·인계는 각자의 모듈이 소유하는 별도
    상태 축이므로 이 컬럼에 들어오지 않는다(19 §6은 이들을 하나의 enum으로
    뭉개지 말라고 경고한다).
    """

    __tablename__ = "fulfillment_request"
    __table_args__ = (
        # AC-02의 최종 보증. 애플리케이션 조회로는 동시 접수를 막을 수 없다.
        # 이 제약이 한쪽을 실패시키고, 패자는 다시 읽어 기존 결과를 반환한다.
        UniqueConstraint("seller_id", "external_request_id", name="uq_request_seller_external_id"),
        CheckConstraint(f"status IN ('{REQUEST_STATUS_ACCEPTED}')", name="ck_request_status"),
        # ADR23-08을 DB가 강제한다. 실운영 데이터를 담은 INSERT는 권고가 아니라
        # 거부로 막힌다.
        CheckConstraint(
            f"environment_class IN ('{ENVIRONMENT_SIMULATION}')",
            name="ck_request_environment_class",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    seller_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("seller.id"))
    external_request_id: Mapped[str] = mapped_column(String(100))

    # 정규화된 내용의 해시와 규칙 버전. 규칙이 바뀌어도 이전·이후 fingerprint가
    # 비교 가능해지지 않는다.
    fingerprint: Mapped[str] = mapped_column(String(64))
    normalization_version: Mapped[int] = mapped_column(SmallInteger)

    status: Mapped[str] = mapped_column(String(30), default=REQUEST_STATUS_ACCEPTED)
    environment_class: Mapped[str] = mapped_column(String(20), default=ENVIRONMENT_SIMULATION)

    # SIMULATION 데이터만. 실제 수취인 정보는 쓰지 않는다(R-08 미승인).
    recipient_name: Mapped[str] = mapped_column(String(100))
    recipient_address: Mapped[str] = mapped_column(Text)
    recipient_phone: Mapped[str] = mapped_column(String(40))

    # 약속이 아니라 선호이므로 없을 수 있다.
    desired_ship_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 일부러 분리한다. 합쳐 두면 기한을 재협의할 때 최초 약속을 덮어쓰는 코드가
    # 자연스럽게 작성되는데, AC-23이 그것을 금지한다.
    initial_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # 언제 일어났는지, 언제 수신했는지, 언제 기록했는지. 서로 다른 사실이며
    # 사건이 지난 뒤에는 분리할 수 없다.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RequestLine(Base):
    """요청 안의 SKU 하나와 수량."""

    __tablename__ = "request_line"
    __table_args__ = (
        # 한 요청에 같은 SKU가 두 번 있으면 FIFO 순번이 어느 라인에 속하는지
        # 모호해진다. NBR-06은 구성 변경을 취소 후 신규 요청으로 처리하므로
        # 중복 라인이 필요한 경우는 없다.
        UniqueConstraint("request_id", "sku_id", name="uq_line_request_sku"),
        CheckConstraint("quantity > 0", name="ck_line_quantity_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fulfillment_request.id"))
    sku_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sku.id"))
    quantity: Mapped[int] = mapped_column(Integer)


class DemandSegment(Base):
    """FIFO 순번 하나를 갖는 수요 구간.

    배정은 요청이 아니라 구간 단위로 일어난다. 수량을 늘리면 원래 구간을 키우는
    대신 새로운 후순위 구간이 생기고(NBR-05), 줄이면 최신 구간부터 차감한다
    (D-01).

    잔량 컬럼은 없다. 미충족 수량은 배정의 관심사이며, 지금 만들면 아무도
    갱신하지 않는 컬럼이 된다.
    """

    __tablename__ = "demand_segment"
    __table_args__ = (
        # FIFO 순번 중복을 DB가 거부한다. 순번 발급에 버그가 생겨도 역전된 큐가
        # 데이터로 남지 않는다.
        UniqueConstraint("sku_id", "seq", name="uq_segment_sku_seq"),
        CheckConstraint("quantity > 0", name="ck_segment_quantity_positive"),
        CheckConstraint(
            f"kind IN ('{DEMAND_KIND_ORIGINAL}', '{DEMAND_KIND_INCREASE}')",
            name="ck_segment_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    line_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("request_line.id"))
    # line_id를 타면 알 수 있지만, FIFO 순번의 유일성을 제약으로 표현하기 위해
    # 여기 중복해 둔다.
    sku_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sku.id"))
    # allocation guard가 발급한다. 타임스탬프나 자동 증가 id가 아니라 이 값만이
    # FIFO 순서의 근거다.
    seq: Mapped[int] = mapped_column(BigInteger)
    quantity: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

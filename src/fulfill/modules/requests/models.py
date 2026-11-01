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
        # > 0이 아니라 >= 0이다. 감량이 증량 구간을 전부 지우면 그 구간은 0이 된다.
        # 행을 삭제할 수는 없다 — allocation이 이 구간을 참조하고 있고, 지우면
        # AC-36이 요구하는 배정 이력을 잃는다. 24-2에서 미검수 버킷이 0으로 남는
        # 것과 같은 이유이고 같은 해법이다.
        #
        # request_line.quantity는 여전히 > 0이다. 라인이 0이 되는 것은 감량이
        # 아니라 SKU 구성 변경이며, NBR-06이 취소 후 신규 요청으로 처리하라고
        # 정했다. 두 제약이 다른 것은 의도다.
        CheckConstraint("quantity >= 0", name="ck_segment_quantity_not_negative"),
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


class RequestRevision(Base):
    """수량 변경 사실 하나. 바뀐 라인마다 한 행이다 (AC-03).

    19 §3이 RequestRevision을 Immutable fact로 분류했다. 수요 구간과 라인 수량은
    갱신되지만 "누가 언제 무엇을 왜 바꿨는가"는 여기 쌓인다. 24-2의 qc_decision·
    ownership_review와 같은 구조다 — 변하는 상태와 변하지 않는 사실을 나눈다.

    이전 수량을 함께 적는다. 현재 수량만 알면 무엇이 바뀌었는지 알 수 없고,
    AC-03이 "요청 및 변경 접수 순서·구간별 수요/배정/감소 사유"를 업무 근거로
    요구한다.

    기한 변경은 여기 들어오지 않는다. AC-23이 최초 기한 보존을 따로 요구하며
    별도 사건이다.
    """

    __tablename__ = "request_revision"
    __table_args__ = (
        CheckConstraint("previous_quantity > 0", name="ck_revision_previous_quantity_positive"),
        # 0으로 줄이는 것은 SKU 구성 변경이다(NBR-06). 도메인이 거절하지만 DB도
        # 막아, 다른 경로가 생겨도 그 상태가 데이터로 남지 않는다.
        CheckConstraint("new_quantity > 0", name="ck_revision_new_quantity_positive"),
        # 바뀌지 않은 변경은 사실이 아니다. 조정 계층이 차이가 0인 라인을
        # 건너뛰므로 여기 도달하면 버그다.
        CheckConstraint("previous_quantity <> new_quantity", name="ck_revision_quantity_changed"),
        CheckConstraint(
            f"environment_class IN ('{ENVIRONMENT_SIMULATION}')",
            name="ck_revision_environment_class",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 한 변경 명령이 바꾼 라인들을 묶는다. FK가 아니다 — 묶음 자체는 별도 객체가
    # 아니고, TX-02가 요구하는 원자성은 트랜잭션이 보장한다. 이 값은 나중에
    # "그때 그 명령이 무엇을 바꿨는가"를 되읽기 위한 것이다.
    group_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fulfillment_request.id"))
    line_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("request_line.id"))

    previous_quantity: Mapped[int] = mapped_column(Integer)
    new_quantity: Mapped[int] = mapped_column(Integer)

    # 권한 확인은 하지 않는다. 판매사별 변경 권한자는 R-05/06이 미확정이고 AC-35가
    # 수동 확인을 전제하므로, 누가 바꿨다고 주장했는지만 기록한다.
    revised_by: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text)

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    environment_class: Mapped[str] = mapped_column(String(20), default=ENVIRONMENT_SIMULATION)

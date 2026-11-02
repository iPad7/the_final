"""입고·검수·재고 ORM 모델 (BC-03)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

# 업무 어휘는 domain이 소유한다. 여기서 다시 정의하면 CHECK 제약과 도메인
# 판정이 갈라질 수 있다.
from fulfill.modules.inventory.domain import (
    HOLD_REASONS,
    OWNERSHIP_CONFIRMED,
    OWNERSHIP_UNKNOWN,
    QC_FAIL,
    QC_PASS,
    QC_STATE_FAILED,
    QC_STATE_PASSED,
    QC_STATE_PENDING,
    REVIEW_OUTCOMES,
)
from fulfill.shared.db import Base

# 이것은 업무 어휘가 아니라 운영 통제 장치라서 domain으로 올리지 않는다.
# requests 모듈에도 같은 값이 있다. 모듈 간 ORM import는 경계 계약이 금지하므로
# 피할 수 없는 중복이며, 두 값이 어긋나면 깨지는 테스트를 둔다.
ENVIRONMENT_SIMULATION = "SIMULATION"


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
        # 새 규칙이 아니다. id가 PK이므로 이 쌍은 이미 항상 유일하다. receipt의
        # 복합 FK가 "이 SKU가 정말 그 판매사 것인지"를 검사할 수 있도록 기존
        # 사실을 유니크 인덱스로 노출하는 것뿐이다.
        UniqueConstraint("seller_id", "id", name="uq_sku_seller_scoped_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    seller_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("seller.id"))
    code: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InboundAdvice(Base):
    """판매사가 통보한 입고 예정.

    예정량은 약속이지 물건이 아니다. AC-06은 예정 600개를 물리 정상 재고나 검수
    합격으로 취급하지 말라고 요구하므로, 이 테이블은 재고 수량을 전혀 갖지 않고
    실도착은 `receipt`가 따로 기록한다.

    유니크 제약을 두지 않는다. 같은 SKU에 예정 통보가 여러 번 오는 것은 분할
    입고라는 정상 업무이며, 막으면 실제 업무를 표현할 수 없게 된다.

    예정 대비 부족분 컬럼도 없다. 예정량과 실도착 합계에서 계산되는 값이므로
    저장하면 세 번째 진실원이 되고, 분할 입고가 추가될 때마다 갱신을 누락한다.
    """

    __tablename__ = "inbound_advice"
    __table_args__ = (CheckConstraint("expected_quantity > 0", name="ck_advice_quantity_positive"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # seller_id를 두지 않는다. SKU가 이미 정확히 한 판매사에 속하므로 두 값이
    # 어긋날 여지만 생긴다(SkuDemandGuard와 같은 판단).
    sku_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sku.id"))
    expected_quantity: Mapped[int] = mapped_column(Integer)

    # 판매사가 통보한 시각과 우리가 적은 시각은 다른 사실이다. 사건이 지난 뒤에는
    # 둘을 다시 분리할 수 없다.
    advised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Receipt(Base):
    """입고 담당자가 확인한 실도착 한 건.

    실도착과 소유 확인은 다른 사실이다(CLAUDE.md 6장). 그래서 `seller_id`와
    `sku_id`가 nullable이다. AC-08의 소유자 미식별 입고품은 어느 판매사의 어느
    품목인지 모르는 실물이고, `sku.seller_id`가 NOT NULL이므로 SKU를 지목할 수
    없다. 귀속을 필수로 두면 이 업무 상황을 아예 표현할 수 없다.

    귀속 미확정 입고품은 `stock_position` 버킷을 가질 수 없다. 버킷의 `sku_id`가
    NOT NULL이기 때문이다. 그래서 AC-08의 "정상 재고 배정을 막는다"가 별도 분기
    없이 성립한다 — 배정 조회가 볼 수 있는 곳에 수량이 존재하지 않는다.

    유니크 제약은 없다. 한 예정 통보에 여러 실도착이 붙는 것이 분할 입고다.
    """

    __tablename__ = "receipt"
    __table_args__ = (
        # 재고 섞임을 DB가 막는다. PostgreSQL의 기본 MATCH SIMPLE은 참조 컬럼 중
        # 하나라도 NULL이면 검사를 건너뛰므로, 귀속이 부분적인 행은 통과하고
        # (판매사 A, B사의 품목)만 거부된다. 정보가 불완전할 때는 허용하고
        # 완전할 때는 검증하는 셈이다.
        ForeignKeyConstraint(
            ["seller_id", "sku_id"],
            ["sku.seller_id", "sku.id"],
            name="fk_receipt_seller_sku",
        ),
        CheckConstraint(
            f"ownership_status IN ('{OWNERSHIP_CONFIRMED}', '{OWNERSHIP_UNKNOWN}')",
            name="ck_receipt_ownership_status",
        ),
        # 위 복합 FK가 통과시키는 마지막 구멍을 막는다. 품목을 알면 판매사도
        # 아는 것이 당연한데, (NULL, 품목)은 MATCH SIMPLE이 검사하지 않는다.
        CheckConstraint(
            "sku_id IS NULL OR seller_id IS NOT NULL",
            name="ck_receipt_sku_implies_seller",
        ),
        # CONFIRMED는 "귀속을 지목했다"는 주장이다. 반대 방향은 강제하지 않는다.
        # UNKNOWN에 둘 다 NULL을 요구하면 판매사만 알아낸 중간 상태를 표현할 수
        # 없어진다.
        CheckConstraint(
            f"ownership_status <> '{OWNERSHIP_CONFIRMED}'"
            " OR (seller_id IS NOT NULL AND sku_id IS NOT NULL)",
            name="ck_receipt_confirmed_attribution",
        ),
        CheckConstraint("arrived_quantity > 0", name="ck_receipt_quantity_positive"),
        # ADR23-08을 DB가 강제한다. 이 행은 사람이 실물을 확인했다는 불변 사실
        # 이므로, 데모 기록이 운영 증빙으로 읽히는 길을 거부로 막는다.
        CheckConstraint(
            f"environment_class IN ('{ENVIRONMENT_SIMULATION}')",
            name="ck_receipt_environment_class",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 예정 통보 없이 도착한 물건도 있다. AC-08의 미확인품에는 붙일 예정이 없다.
    advice_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("inbound_advice.id"), nullable=True
    )

    # sku_id에 개별 FK를 걸지 않는다. 위 복합 FK와 ck_receipt_sku_implies_seller가
    # 합쳐져 sku_id가 NOT NULL인 모든 행을 이미 검증하므로, 개별 FK는 같은 일을
    # 하는 두 번째 제약과 인덱스만 만든다.
    seller_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("seller.id"), nullable=True)
    sku_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # 확인할 수 없으면 UNKNOWN으로 남긴다. 안전한 쪽을 기본값으로 둔다.
    ownership_status: Mapped[str] = mapped_column(String(20), default=OWNERSHIP_UNKNOWN)

    arrived_quantity: Mapped[int] = mapped_column(Integer)
    # AC-06이 요구하는 "입고 담당 확인". 누가 셌는지가 업무 근거다.
    confirmed_by: Mapped[str] = mapped_column(String(100))
    environment_class: Mapped[str] = mapped_column(String(20), default=ENVIRONMENT_SIMULATION)

    # 물건이 도착한 시각과 우리가 적은 시각은 다르다.
    arrived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class QcDecision(Base):
    """검수자가 내린 품질 판정 한 건.

    추가만 하고 수정하지 않는다. AC-36은 사후 변경으로 과거 사실을 덮어쓰지
    말라고 요구하므로, 판정을 되돌릴 때는 이 행을 고치는 대신 새 판정을 쌓는다.

    유니크 제약이 없는 것이 핵심이다. 한 입고에 판정이 여러 번 붙는 것이 바로
    부분 QC다(AC-07의 합격 570 + 불합격 20 = 판정 2건). 하나로 제한하면 "나머지
    검수가 끝나기를 기다리지 않는다"는 NBR-11을 표현할 수 없다.

    격리 구역은 여기 적지 않는다. "20개가 불합격이다"는 판정이고 "격리 구역에
    20개가 있다"는 실물 소재이므로, 후자는 stock_position이 가진다.
    """

    __tablename__ = "qc_decision"
    __table_args__ = (
        CheckConstraint(f"result IN ('{QC_PASS}', '{QC_FAIL}')", name="ck_qc_result"),
        CheckConstraint("quantity > 0", name="ck_qc_quantity_positive"),
        # 사람이 실물을 검수했다는 불변 사실이므로 환경을 DB가 강제한다(ADR23-08).
        CheckConstraint(
            f"environment_class IN ('{ENVIRONMENT_SIMULATION}')",
            name="ck_qc_environment_class",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 판정의 대상은 버킷이 아니라 입고다. 버킷은 이 판정을 적용한 결과로 바뀌는
    # 파생 상태이므로, 사실이 파생물을 가리키면 방향이 뒤집힌다.
    receipt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("receipt.id"))
    result: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[int] = mapped_column(Integer)
    # AC-07이 요구하는 "검수 주체".
    decided_by: Mapped[str] = mapped_column(String(100))
    environment_class: Mapped[str] = mapped_column(String(20), default=ENVIRONMENT_SIMULATION)

    # AC-07이 요구하는 "판정 시각". 기록 시각과 구분한다.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OwnershipReview(Base):
    """소유 귀속을 확인하려 한 시도 하나.

    AC-08은 미확인품을 막는 것만으로 끝나지 않고 "공통 기준에 따른 확인·재확인·
    관리자 보고를 기록"하라고 요구한다. receipt.ownership_status는 현재 상태만
    담으므로, 몇 번 시도했고 기한이 언제였고 관리자가 어떤 판단을 했는지는 여기
    쌓인다.

    추가만 하고 수정하지 않는다. 재확인은 같은 행을 고치는 일이 아니라 새 사실이
    생기는 일이다(AC-11·AC-36).

    due_at에 그때 적용한 기한을 적는다. "운영사 공통 확인 기한"의 실제 값은 아직
    미확정 운영 파라미터(R-05/06)이므로 코드에 박지 않고 사실로 기록한다. 나중에
    계약값이 정해져도 과거 기록이 거짓이 되지 않는다.
    """

    __tablename__ = "ownership_review"
    __table_args__ = (
        CheckConstraint(
            f"outcome IN ({', '.join(repr(value) for value in REVIEW_OUTCOMES)})",
            name="ck_review_outcome",
        ),
        # 사람이 내린 판단을 적는 불변 사실이므로 환경을 DB가 강제한다(ADR23-08).
        CheckConstraint(
            f"environment_class IN ('{ENVIRONMENT_SIMULATION}')",
            name="ck_review_environment_class",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    receipt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("receipt.id"))
    outcome: Mapped[str] = mapped_column(String(20))
    reviewed_by: Mapped[str] = mapped_column(String(100))
    # 확인하지 못한 이유, 관리자 판단 근거. 길이를 모르므로 Text로 둔다.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 이 시도에 적용한 운영사 공통 확인 기한. 기한을 적용하지 않은 시도도 있다.
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    environment_class: Mapped[str] = mapped_column(String(20), default=ENVIRONMENT_SIMULATION)

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InventoryHold(Base):
    """입고묶음 하나에 걸린 사용 보류 한 건 (AC-04, AC-10).

    범위 단위가 receipt인 것이 이 테이블의 전제다. NBR-14는 "영향 가능 범위"만
    보류하라고 요구하고 AC-10은 무관 재고 30개의 자동 차단을 금지한다. 영향
    범위를 가르는 단위는 입고묶음이므로(19 §3), 같은 SKU의 다른 입고묶음은 이
    보류에 걸리지 않는다.

    stock_position의 컬럼이 아니라 별 테이블인 것이 설계의 핵심이다. 버킷에
    HOLD를 넣으면 보류가 수량 분할이 되어, 이미 배정된 재고도 사용 보류일 수
    있다는 CB-04의 요구를 표현할 수 없다. 여기서는 보류와 유효 배정이 공존하며,
    보류가 걸려도 기존 배정은 사라지지 않는다 — AC-04가 "조사 의심 단계에서
    유효 배정을 무단 삭제하지 않는다"고 금지한 것이다.

    해제를 같은 행에 채운다. allocation_release처럼 별 테이블을 두지 않는 이유는
    부분 해제라는 개념이 없기 때문이다. 한 보류는 한 번 풀리고, 다시 보류하는
    것은 같은 사실의 연장이 아니라 새 사실이므로 새 행이 된다.
    """

    __tablename__ = "inventory_hold"
    __table_args__ = (
        CheckConstraint(
            f"reason IN ({', '.join(repr(value) for value in HOLD_REASONS)})",
            name="ck_hold_reason",
        ),
        # 해제의 세 사실은 함께 생긴다. 하나만 채워진 행은 "언제 풀렸는지 모르는
        # 해제"나 "누가 풀었는지 모르는 해제"이며 AC-36의 판단 주체·시점 보존을
        # 만족하지 못한다. 활성 판정이 lifted_at 하나만 보므로, 이 제약이 없으면
        # 주체 없이 풀린 보류가 조용히 활성에서 빠진다.
        CheckConstraint(
            "(lifted_at IS NULL AND lifted_by IS NULL AND lifted_recorded_at IS NULL)"
            " OR (lifted_at IS NOT NULL AND lifted_by IS NOT NULL"
            " AND lifted_recorded_at IS NOT NULL)",
            name="ck_hold_lift_fields_together",
        ),
        # 사람이 내린 판단을 적는 불변 사실이므로 환경을 DB가 강제한다(ADR23-08).
        CheckConstraint(
            f"environment_class IN ('{ENVIRONMENT_SIMULATION}')",
            name="ck_hold_environment_class",
        ),
        # 한 입고묶음에 활성 보류는 하나다. 해제된 행은 몇 개든 쌓인다.
        #
        # 부분 유니크 인덱스라서 조건을 만족하는 행들 사이에서만 유일성을 본다.
        # 조회해서 없으면 넣는 방식으로 중복을 막으면 확인과 삽입 사이에 다른
        # 트랜잭션이 끼어들 수 있다(24-1의 멱등 경로와 같은 문제다).
        Index(
            "uq_hold_active_per_receipt",
            "receipt_id",
            unique=True,
            postgresql_where=text("lifted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    receipt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("receipt.id"))
    reason: Mapped[str] = mapped_column(String(30))
    # AC-10이 요구하는 "조사 근거". 길이를 모르므로 Text로 둔다.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    held_by: Mapped[str] = mapped_column(String(100))
    environment_class: Mapped[str] = mapped_column(String(20), default=ENVIRONMENT_SIMULATION)

    # 보류가 실제로 시작된 시각과 기록된 시각을 구분한다.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # NULL이면 활성 보류다. 상태 컬럼을 따로 두지 않는 이유는 24-3에서 미충족
    # 수량에 컬럼을 두지 않은 것과 같다 — 갱신을 누락할 자리를 만들지 않는다.
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lifted_recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lifted_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lifted_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class LocationConfirmation(Base):
    """합격 재고의 실제 피킹 위치를 확인한 사건 하나 (AC-09).

    버킷의 location_code는 제자리 갱신되므로 확인 전 값이 남지 않는다. 그래서
    previous_location_code를 여기 적는다. AC-09가 업무 근거로 요구하는 것이
    "임시 보관 위치"와 "실제 피킹 위치 확인 사실" 둘이므로, 확정된 위치만 남기면
    절반만 보존된다.

    추가만 하고 수정하지 않는다. 같은 재고가 나중에 다른 자리로 옮겨지면 이 행을
    고치는 대신 새 사건을 쌓는다(AC-36).

    대상이 버킷이 아니라 입고묶음인 것은 qc_decision과 같은 이유다. 버킷은 이
    사건을 적용한 결과로 바뀌는 파생 상태이므로, 사실이 파생물을 가리키면 방향이
    뒤집힌다. 다만 어느 버킷이 바뀌었는지는 추적해야 하므로 stock_position_id를
    함께 적는다 — 제자리 갱신이라 버킷 id가 변하지 않는 덕분에 이 참조가 계속
    유효하다.
    """

    __tablename__ = "location_confirmation"
    __table_args__ = (
        # 확인 전과 후가 같으면 확인이 아니다. 위치가 이미 그 값이었다는 뜻이고,
        # 조정 계층이 그 경우를 변화 없음으로 판정하므로 여기 도달하지 않는다.
        CheckConstraint(
            "previous_location_code IS NULL OR previous_location_code <> location_code",
            name="ck_location_confirmation_changes_location",
        ),
        # 사람이 실물 위치를 확인했다는 불변 사실이므로 환경을 DB가 강제한다
        # (ADR23-08).
        CheckConstraint(
            f"environment_class IN ('{ENVIRONMENT_SIMULATION}')",
            name="ck_location_confirmation_environment_class",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    receipt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("receipt.id"))
    stock_position_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stock_position.id"))
    # 확인 전 위치. NULL이면 임시 구역에 있었고 피킹 위치가 미확정이었다는 뜻이며,
    # AC-09의 출발 상태가 바로 이것이다.
    previous_location_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    location_code: Mapped[str] = mapped_column(String(100))
    confirmed_by: Mapped[str] = mapped_column(String(100))
    environment_class: Mapped[str] = mapped_column(String(20), default=ENVIRONMENT_SIMULATION)

    # 실제로 확인한 시각과 기록된 시각을 구분한다.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class StockPosition(Base):
    """같은 조건의 실물을 한 행으로 모은 재고 버킷.

    굿즈에는 개체 식별자가 없다. 같은 SKU의 피규어 590개는 서로 구별되지 않으므로
    590행을 만들면 존재하지 않는 구별을 데이터로 꾸미는 일이 된다. 19단계가 남긴
    미결 질문(개별 실물 식별 vs lot/수량)에 수량 방식으로 답한 것이다.

    키에 qc_state를 넣은 것이 설계의 핵심이다. 그러면 QC 판정이 "버킷 사이 수량
    이동"이 되고, PENDING에 남은 양이 곧 AC-07의 미검수 수량이 된다. 별도 컬럼
    없이 세 수량(합격·불합격·미검수)이 서로 배타적으로 유지되므로, CB-04가
    경고한 "onHold+allocated+picked를 더해 총수량을 구하는" 이중 계산이 구조적으로
    불가능하다. 확인된 입고 하나의 버킷 수량 합은 항상 실도착 수량과 같다.

    HOLD는 키에 넣지 않는다. 넣으면 HOLD가 수량 분할이 되어, 이미 배정된 재고도
    사용 보류일 수 있다는 CB-04의 요구를 표현할 수 없다. HOLD는 별 축이다.

    sku_id가 NOT NULL인 것이 AC-08의 관문이다. 귀속 미확정 입고품은 SKU를 지목할
    수 없으므로 버킷을 가질 수 없고, 따라서 배정 조회에 나타날 수 없다.
    """

    __tablename__ = "stock_position"
    __table_args__ = (
        # 같은 조건의 수량은 정확히 한 행에만 있어야 한다.
        #
        # nulls_not_distinct가 없으면 이 제약은 location_code가 NULL인 행을 전혀
        # 막지 못한다. PostgreSQL은 기본적으로 NULL을 서로 다른 값으로 보기
        # 때문이다. 위치 미확정은 입고 직후의 정상 상태이므로, 하필 가장 중요한
        # 시점에 버킷이 쪼개지고 보존 불변식이 무너진다.
        UniqueConstraint(
            "sku_id",
            "receipt_id",
            "qc_state",
            "location_code",
            name="uq_stock_bucket",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            f"qc_state IN ('{QC_STATE_PENDING}', '{QC_STATE_PASSED}', '{QC_STATE_FAILED}')",
            name="ck_stock_qc_state",
        ),
        # > 0이 아니라 >= 0이다. 판정을 전부 적용하면 PENDING 버킷은 0이 된다.
        # 그리고 이 제약이 과검수를 막는다. 실도착 590개에 합격 600개를 판정하려면
        # PENDING을 음수로 만들어야 하므로 DB가 거부한다.
        CheckConstraint("quantity >= 0", name="ck_stock_quantity_not_negative"),
        # 이중 배정을 DB가 막는다. CB-02가 "가장 중요한 재고·배정 불변조건"이라
        # 부른 것을 애플리케이션 코드 하나에 맡기지 않는다.
        #
        # "모든 배정의 합이 재고를 넘지 않는다"는 원래 집계 조건이라 CHECK로 쓸 수
        # 없는 모양이다. 갱신되는 합계 컬럼을 두면 그것이 행 조건으로 번역된다.
        # 24-2에서 과검수를 quantity >= 0으로 막은 것과 같은 수법이다.
        #
        # quantity를 줄이는 쪽도 이 제약에 걸린다. 손실 확정으로 실물이 줄면 먼저
        # 그 요청의 배정을 조정해야 하며(D-09, NBR-13), 배정을 남겨 둔 채 재고만
        # 지우는 순서는 거부된다.
        CheckConstraint(
            "allocated_quantity >= 0 AND allocated_quantity <= quantity",
            name="ck_stock_allocated_within_quantity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    sku_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sku.id"))
    # 영향 범위를 가르는 단위다. AC-10의 "영향 가능한 재고만 사용 보류"를 판정할
    # 때 입고묶음이 그 범위가 된다(19 §3이 StockPosition을 판매사/SKU/입고묶음/
    # 물리 소재별 수량으로 정의한다).
    receipt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("receipt.id"))
    qc_state: Mapped[str] = mapped_column(String(20))
    # NULL이 곧 "피킹 위치 미확정"이다. 그래서 NBR-12의 배정 가능·피킹 불가가
    # 추가 상태 없이 나온다. 불합격품의 격리 구역도 이 컬럼에 담긴다.
    location_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer)

    # 이 버킷 수량 중 이미 어떤 수요 구간에 독점 배정된 양. 배정 사실의 원장은
    # allocation 테이블이고 이 컬럼은 그 합계다. 둘이 어긋나면 깨지는 불변식
    # 테스트를 둔다(24-2의 "버킷 합 == 실도착 수량"과 같은 성격이다).
    #
    # 19 §3이 StockPosition을 "수량과 사용가능 여부"로 정의하므로 이 컬럼은
    # inventory의 소유물이다. allocation 모듈은 inventory가 공개한 함수를 통해서만
    # 이 값을 바꾼다.
    #
    # HOLD는 여기 더하지 않는다. CB-04가 "onHold+allocated+picked를 단순 합산하면
    # 중복 계산된다"고 경고했고, 이미 배정된 물량도 사용 보류일 수 있다. HOLD는
    # 별 축이며 24-4의 일이다.
    allocated_quantity: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

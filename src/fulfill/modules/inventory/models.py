"""입고·검수·재고 ORM 모델 (BC-03)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

# 업무 어휘는 domain이 소유한다. 여기서 다시 정의하면 CHECK 제약과 도메인
# 판정이 갈라질 수 있다.
from fulfill.modules.inventory.domain import (
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

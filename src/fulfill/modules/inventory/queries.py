"""BC-03이 공개하는 조회 입구."""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, SQLColumnExpression, func, select
from sqlalchemy.orm import Session

from fulfill.modules.inventory.domain import QC_STATE_PASSED
from fulfill.modules.inventory.models import (
    InboundAdvice,
    InventoryHold,
    Receipt,
    Sku,
    StockPosition,
)


def active_hold_exists(receipt_id: SQLColumnExpression[uuid.UUID]) -> ColumnElement[bool]:
    """이 입고묶음에 풀리지 않은 사용 보류가 있는가 (AC-04, NBR-14).

    질의 조건으로 쓸 식만 만든다. 배정 자격을 판정하는 곳이 두 군데(잠금과 가용량
    조회)이고, 조건을 양쪽에 따로 쓰면 한쪽만 고쳐질 수 있다. 그때 생기는 상태는
    "현황에는 0인데 배정은 되는" 또는 그 반대이며, 둘 다 담당자가 재고를 신뢰할 수
    없게 만든다.

    활성 여부를 `lifted_at IS NULL`로 판정한다. 상태 컬럼이 없으므로 해제 사실과
    활성 여부가 어긋날 자리가 없다.
    """
    return (
        select(InventoryHold.id)
        .where(
            InventoryHold.receipt_id == receipt_id,
            InventoryHold.lifted_at.is_(None),
        )
        .exists()
    )


def find_sku_ids_by_codes(
    session: Session, seller_id: uuid.UUID, codes: Iterable[str]
) -> dict[str, uuid.UUID]:
    """판매사의 품목 코드들을 한 번의 질의로 해석한다.

    대응하는 SKU가 없는 코드는 결과에서 빠진다. 호출자는 그것으로 잘못된 요청
    라인을 알아낸다.
    """
    rows = session.execute(
        select(Sku.code, Sku.id).where(Sku.seller_id == seller_id, Sku.code.in_(list(codes)))
    ).all()
    return {code: sku_id for code, sku_id in rows}


def find_sku_codes_by_ids(session: Session, sku_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    """SKU id를 사람이 읽을 코드로 되돌린다.

    `find_sku_ids_by_codes`의 역방향이다. 다른 모듈이 들고 있는 sku_id를 화면이나
    응답에 보여 줄 때 쓴다. Sku는 BC-03의 소유이므로 이 변환을 다른 모듈이 직접
    할 수 없다.

    판매사로 좁히지 않는다. id는 이미 판매사까지 특정하므로 추가 조건이 필요 없다.
    """
    rows = session.execute(select(Sku.id, Sku.code).where(Sku.id.in_(list(sku_ids)))).all()
    return {sku_id: code for sku_id, code in rows}


def find_allocatable_quantity(session: Session, sku_id: uuid.UUID) -> int:
    """이 SKU에서 신규 배정 자격이 있는 수량 (AC-07).

    24-3의 FIFO 배정이 쓸 입구다. AC-07의 판정 기준 — 합격분만 배정 대상 — 이
    이 질의의 조건에 들어 있어야 한다.

    조건이 qc_state 하나뿐인 것이 결정 2의 효과다. 미검수와 불합격은 각자 다른
    버킷에 있으므로 따로 뺄 필요가 없고, 귀속 미확정 물량은 버킷 자체가 없으므로
    애초에 보이지 않는다(AC-08). 상태를 컬럼으로 뒀다면 세 조건을 모두 써야 했고,
    하나를 빠뜨리면 불량품이 배정 가능으로 계산됐을 것이다.

    위치를 조건에 넣지 않는다. NBR-12는 QC 합격 후 위치가 미정이어도 배정은
    가능하다고 요구하므로, 여기서 위치를 거르면 AC-09를 위반한다. 위치 관문은
    피킹 판정의 일이다.

    기존 배정량을 뺀다. 빼지 않으면 이미 다른 요청이 가져간 수량이 다시 배정
    가능으로 보이고, 그것이 NBR-04가 금지하는 중복 배정의 입구가 된다.

    사용 보류가 걸린 입고묶음은 제외한다. `lock_allocatable_buckets`와 같은 식을
    쓰므로 두 곳의 자격 판정이 갈라지지 않는다. 다만 **이미 붙어 있는 배정은 이
    수치에서 빠지는 것과 무관하게 유효하다** — 보류는 신규 배정을 막는 조건이고
    기존 배정을 취소하는 사건이 아니다(AC-04).

    잠그지 않는다. 이것은 현황을 보여 주는 조회이고, 실제 배정은 버킷을 잠그는
    `lock_allocatable_buckets`를 쓴다. 조회 결과를 근거로 쓰기를 결정하면 읽은
    값과 쓸 때의 값이 달라질 수 있다.
    """
    return session.execute(
        select(
            func.coalesce(func.sum(StockPosition.quantity - StockPosition.allocated_quantity), 0)
        ).where(
            StockPosition.sku_id == sku_id,
            StockPosition.qc_state == QC_STATE_PASSED,
            ~active_hold_exists(StockPosition.receipt_id),
        )
    ).scalar_one()


def receipt_exists(session: Session, receipt_id: uuid.UUID) -> bool:
    """그런 입고묶음이 있는가.

    보류·해제 판정이 쓴다. 없는 입고묶음에 보류를 걸면 FK가 거부해 담당자는 업무
    사유 대신 서버 오류를 받고, 해제에서는 "그런 입고묶음이 없다"와 "보류가
    없다"가 서로 다른 결론이므로 이 구분이 필요하다.
    """
    return session.execute(select(Receipt.id).where(Receipt.id == receipt_id)).first() is not None


@dataclass(frozen=True)
class ActiveHold:
    """풀리지 않은 사용 보류 한 건의 업무 근거 (AC-10)."""

    hold_id: uuid.UUID
    reason: str
    note: str | None
    held_by: str
    occurred_at: datetime


def find_active_hold(session: Session, receipt_id: uuid.UUID) -> ActiveHold | None:
    """입고묶음의 활성 보류. None이면 보류가 없다 (AC-10).

    해제된 보류는 돌려주지 않는다. 과거 보류 이력은 지워지지 않고 남아 있지만,
    "지금 이 재고를 쓸 수 있는가"를 묻는 질문의 답은 활성 보류뿐이다.

    활성 보류가 하나임은 부분 유니크 인덱스가 보장하므로 여러 건을 다루지 않는다.
    """
    row = session.execute(
        select(
            InventoryHold.id,
            InventoryHold.reason,
            InventoryHold.note,
            InventoryHold.held_by,
            InventoryHold.occurred_at,
        ).where(
            InventoryHold.receipt_id == receipt_id,
            InventoryHold.lifted_at.is_(None),
        )
    ).first()
    if row is None:
        return None
    return ActiveHold(
        hold_id=row[0],
        reason=row[1],
        note=row[2],
        held_by=row[3],
        occurred_at=row[4],
    )


@dataclass(frozen=True)
class BucketQuantity:
    """재고 버킷 하나의 상태·위치·수량."""

    qc_state: str
    location_code: str | None
    quantity: int


@dataclass(frozen=True)
class ReceiptSummary:
    """입고 하나의 검수 현황 (AC-07).

    세 수량을 함께 돌려주는 것이 중요하다. 하나씩 조회하게 두면 호출자가 합격
    570만 보고 미검수 20을 놓칠 수 있고, 그것이 "불량을 가용으로 계산"이라는
    AC-07의 위반 조항으로 가는 길이다.
    """

    arrived_quantity: int
    ownership_status: str
    buckets: tuple[BucketQuantity, ...]

    def quantity_in(self, qc_state: str) -> int:
        """해당 검수 상태의 총 수량. 버킷이 여러 위치로 나뉘어 있어도 합산한다."""
        return sum(bucket.quantity for bucket in self.buckets if bucket.qc_state == qc_state)

    @property
    def bucket_total(self) -> int:
        """버킷 수량의 합.

        귀속이 확인된 입고에서는 이 값이 실도착 수량과 같아야 한다(결정 2의 보존
        불변식). 미확인 입고는 버킷이 없으므로 0이다.
        """
        return sum(bucket.quantity for bucket in self.buckets)


def summarize_receipt(session: Session, receipt_id: uuid.UUID) -> ReceiptSummary | None:
    """입고의 실도착 수량·귀속 상태·버킷 내역을 돌려준다 (AC-07)."""
    row = session.execute(
        select(Receipt.arrived_quantity, Receipt.ownership_status).where(Receipt.id == receipt_id)
    ).one_or_none()
    if row is None:
        return None

    buckets = session.execute(
        select(
            StockPosition.qc_state,
            StockPosition.location_code,
            StockPosition.quantity,
        )
        .where(StockPosition.receipt_id == receipt_id)
        .order_by(StockPosition.qc_state, StockPosition.location_code.nulls_first())
    ).all()
    return ReceiptSummary(
        arrived_quantity=row[0],
        ownership_status=row[1],
        buckets=tuple(
            BucketQuantity(qc_state=b[0], location_code=b[1], quantity=b[2]) for b in buckets
        ),
    )


@dataclass(frozen=True)
class AdviceSummary:
    """예정 입고 하나의 현황.

    shortfall을 저장하지 않고 계산한다(결정 3). 예정량과 실도착 합계가 이미
    사실이므로, 차이를 컬럼으로 두면 분할 입고가 들어올 때마다 갱신을 누락한다.
    """

    expected_quantity: int
    arrived_quantity: int

    @property
    def shortfall(self) -> int:
        """예정 대비 부족분. 예정보다 많이 왔으면 0이다 (AC-06)."""
        return max(self.expected_quantity - self.arrived_quantity, 0)


def summarize_advice(session: Session, advice_id: uuid.UUID) -> AdviceSummary | None:
    """예정량과 실도착 합계를 함께 돌려준다 (AC-06).

    AC-06은 실도착 590과 예정 대비 부족 10을 구별하라고 요구한다. 두 수를 함께
    돌려주면 호출자가 둘을 뭉개 쓸 수 없다.

    여러 실도착을 합산하는 이유는 분할 입고가 정상 업무이기 때문이다.
    """
    row = session.execute(
        select(
            InboundAdvice.expected_quantity,
            func.coalesce(func.sum(Receipt.arrived_quantity), 0),
        )
        .outerjoin(Receipt, Receipt.advice_id == InboundAdvice.id)
        .where(InboundAdvice.id == advice_id)
        .group_by(InboundAdvice.id, InboundAdvice.expected_quantity)
    ).one_or_none()
    if row is None:
        return None
    return AdviceSummary(expected_quantity=row[0], arrived_quantity=int(row[1]))

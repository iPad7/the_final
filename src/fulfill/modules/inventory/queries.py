"""BC-03이 공개하는 조회 입구."""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fulfill.modules.inventory.domain import QC_STATE_PASSED
from fulfill.modules.inventory.models import InboundAdvice, Receipt, Sku, StockPosition


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


def find_allocatable_quantity(session: Session, sku_id: uuid.UUID) -> int:
    """이 SKU에서 신규 배정 자격이 있는 수량 (AC-07).

    24-3의 FIFO 배정이 쓸 입구다. AC-07의 판정 기준 — 합격분만 배정 대상 — 이
    이 질의의 조건에 들어 있어야 한다.

    조건이 qc_state 하나뿐인 것이 결정 2의 효과다. 미검수와 불합격은 각자 다른
    버킷에 있으므로 따로 뺄 필요가 없고, 귀속 미확정 물량은 버킷 자체가 없으므로
    애초에 보이지 않는다(AC-08). 상태를 컬럼으로 뒀다면 세 조건을 모두 써야 했고,
    하나를 빠뜨리면 불량품이 배정 가능으로 계산됐을 것이다.

    위치를 조건에 넣지 않는다. NBR-12는 QC 합격 후 위치가 미확정이어도 배정은
    가능하다고 요구하므로, 여기서 위치를 거르면 AC-09를 위반한다. 위치 관문은
    피킹 판정의 일이다.

    아직 기존 배정량을 빼지 않는다. 배정은 24-3이 도입하므로 지금은 적격 물량만
    답한다.
    """
    return session.execute(
        select(func.coalesce(func.sum(StockPosition.quantity), 0)).where(
            StockPosition.sku_id == sku_id,
            StockPosition.qc_state == QC_STATE_PASSED,
        )
    ).scalar_one()


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

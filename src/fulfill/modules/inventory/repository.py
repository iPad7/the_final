"""inventory 모듈의 영속화 (BC-03).

ORM 객체를 모듈 밖으로 내보내지 않는다. 호출자에게는 판정에 필요한 값만 담은
평범한 데이터 구조를 돌려준다.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from fulfill.modules.inventory.domain import (
    BUCKET_STATE_BY_QC_RESULT,
    OWNERSHIP_CONFIRMED,
    OWNERSHIP_UNKNOWN,
    QC_FAIL,
    QC_STATE_PENDING,
    REVIEW_CONFIRMED,
    AdviceCommand,
    OwnershipReviewCommand,
    QcCommand,
    ReceiptCommand,
)
from fulfill.modules.inventory.models import (
    InboundAdvice,
    OwnershipReview,
    QcDecision,
    Receipt,
    StockPosition,
)
from fulfill.shared.validation import normalize_text

# 버킷 유니크 제약의 이름. 같은 버킷에 수량을 더하는 동작이 이 이름에 의존한다.
BUCKET_CONSTRAINT = "uq_stock_bucket"


@dataclass(frozen=True)
class NewReceipt:
    """실도착 한 건을 기록하는 데 필요한 값 묶음.

    seller_id와 sku_id는 조정 계층이 코드를 해석한 결과다. 둘 다 None이면 귀속
    미확정 입고이며, 이 모듈은 그 판단을 다시 하지 않는다.
    """

    command: ReceiptCommand
    seller_id: uuid.UUID | None
    sku_id: uuid.UUID | None


def insert_advice(session: Session, command: AdviceCommand, sku_id: uuid.UUID) -> uuid.UUID:
    """입고 예정을 기록하고 예정 id를 돌려준다 (AC-06).

    seller_id를 저장하지 않는다. SKU가 이미 정확히 한 판매사에 속하기 때문이다.
    """
    advice = InboundAdvice(
        sku_id=sku_id,
        expected_quantity=command.expected_quantity,
        advised_at=command.advised_at,
    )
    session.add(advice)
    session.flush()
    return advice.id


def _normalized_location(value: str | None) -> str | None:
    """위치 코드를 버킷 키에 넣을 형태로 정리한다.

    빈 문자열을 None으로 되돌리는 것이 핵심이다. ""와 None은 버킷 유니크 키에서
    서로 다른 값이고, ""는 "위치를 지정했다"는 뜻이 아니므로 의미상 None이 맞다.
    """
    return normalize_text(value or "") or None


def _build_receipt_row(new: NewReceipt) -> Receipt:
    """저장할 입고 행을 만든다.

    ownership_status를 sku_id의 유무에서 유도한다. 호출자가 따로 넘기면 두 값이
    어긋날 수 있고, 그것은 ck_receipt_confirmed_attribution이 거부할 상태다.
    DB가 거부할 조합을 애초에 만들 수 없게 둔다.
    """
    command = new.command
    return Receipt(
        advice_id=command.advice_id,
        seller_id=new.seller_id,
        sku_id=new.sku_id,
        ownership_status=(OWNERSHIP_CONFIRMED if new.sku_id is not None else OWNERSHIP_UNKNOWN),
        arrived_quantity=command.arrived_quantity,
        confirmed_by=normalize_text(command.confirmed_by),
        arrived_at=command.arrived_at,
    )


def insert_receipt(session: Session, new: NewReceipt) -> uuid.UUID:
    """실도착을 기록하고 입고 id를 돌려준다 (AC-06, AC-08).

    AC-08의 관문이 아래 조건문 하나다. 귀속이 확정된 입고만 재고 버킷을 갖고,
    미확정 입고는 receipt 행만 남는다. 그러면 배정 조회가 볼 수 있는 곳에 수량이
    존재하지 않으므로 "정상 재고 배정을 막는다"가 별도 분기 없이 성립한다.

    버킷은 항상 PENDING으로 만든다. 실도착은 검수가 아니다(NBR-09, NBR-10).
    여기서 PASSED를 만들 수 있게 두면 "확인 후 QC 없이 정상 재고에 편입"이라는
    AC-08의 위반 상태를 코드가 표현할 수 있게 된다.
    """
    receipt = _build_receipt_row(new)
    session.add(receipt)
    # 버킷이 입고 id를 참조하므로 먼저 id를 확정한다.
    session.flush()

    if new.sku_id is not None:
        session.add(
            StockPosition(
                sku_id=new.sku_id,
                receipt_id=receipt.id,
                qc_state=QC_STATE_PENDING,
                # 위치는 아직 미확정이다. NULL이 그 사실이며, NBR-12의 배정
                # 가능·피킹 불가가 여기서 시작된다. 위치 확인은 별도 작업이다.
                location_code=None,
                quantity=new.command.arrived_quantity,
            )
        )
    return receipt.id


def lock_pending_quantity(session: Session, receipt_id: uuid.UUID) -> int:
    """이 입고의 미검수 잔량을 잠그고 돌려준다 (AC-07).

    FOR UPDATE로 잠그는 이유가 있다. 도메인 검증은 이 값으로 과검수를 거절하는데,
    값을 읽은 뒤 판정을 기록하기까지의 틈에 다른 트랜잭션이 같은 입고를 검수하면
    두 판정의 합이 실도착 수량을 넘을 수 있다. 잠그면 뒤에 온 쪽이 기다렸다가
    갱신된 값을 본다. 24-1의 FIFO 순번 발급과 같은 문제이고 같은 해법이다.

    버킷이 없으면 0이다. 귀속 미확정 입고가 정확히 그 상태이므로 모든 판정이
    "미검수 수량 0개를 넘습니다"로 거절된다.
    """
    quantities = (
        session.execute(
            select(StockPosition.quantity)
            .where(
                StockPosition.receipt_id == receipt_id,
                StockPosition.qc_state == QC_STATE_PENDING,
            )
            .with_for_update()
        )
        .scalars()
        .all()
    )
    return sum(quantities)


def _add_to_bucket(
    session: Session,
    sku_id: uuid.UUID,
    receipt_id: uuid.UUID,
    qc_state: str,
    location_code: str | None,
    quantity: int,
) -> None:
    """목적지 버킷에 수량을 더한다. 없으면 만든다.

    부분 QC는 여러 번 일어나므로 목적지 버킷이 이미 있을 수 있다. ON CONFLICT로
    더하는 쪽이 조회 후 분기보다 왕복이 적고, 동시 판정에서도 한쪽이 덮어쓰지
    않는다.

    이 동작은 uq_stock_bucket이 NULLS NOT DISTINCT여야 성립한다. 위치가 NULL인
    버킷끼리 충돌로 인식되지 않으면 합쳐지지 않고 행이 쌓인다.
    """
    session.execute(
        insert(StockPosition)
        .values(
            sku_id=sku_id,
            receipt_id=receipt_id,
            qc_state=qc_state,
            location_code=location_code,
            quantity=quantity,
        )
        .on_conflict_do_update(
            constraint=BUCKET_CONSTRAINT,
            set_={"quantity": StockPosition.quantity + quantity},
        )
    )


def _locked_pending_buckets(
    session: Session, receipt_id: uuid.UUID
) -> list[tuple[uuid.UUID, uuid.UUID, str | None, int]]:
    """차감할 미검수 버킷을 잠그고 정해진 순서로 돌려준다.

    순서를 고정하는 이유는 24-1의 다중 SKU 잠금과 같다. 순서가 비결정적이면
    같은 입력이 다른 결과를 낼 수 있고 교착 가능성도 생긴다.
    """
    rows = session.execute(
        select(
            StockPosition.id,
            StockPosition.sku_id,
            StockPosition.location_code,
            StockPosition.quantity,
        )
        .where(
            StockPosition.receipt_id == receipt_id,
            StockPosition.qc_state == QC_STATE_PENDING,
            StockPosition.quantity > 0,
        )
        .order_by(StockPosition.location_code.nulls_first(), StockPosition.id)
        .with_for_update()
    ).all()
    return [(row[0], row[1], row[2], row[3]) for row in rows]


def apply_qc_decision(session: Session, command: QcCommand) -> None:
    """검수 판정을 기록하고 그 수량을 버킷 사이로 옮긴다 (AC-07).

    판정은 수량을 만들지도 없애지도 않는다. PENDING에서 빼서 PASSED 또는
    FAILED로 옮길 뿐이다. 그래서 확인된 입고 하나의 버킷 수량 합은 언제나
    실도착 수량과 같다(결정 2의 보존 불변식).

    목적지 위치가 결과에 따라 다르다. 합격품은 움직이지 않으므로 원래 자리를
    유지하고, 불합격품은 명령이 지정한 격리 구역으로 간다(AC-07).

    호출 전에 lock_pending_quantity로 잠그고 도메인 검증을 통과해야 한다.
    """
    session.add(
        QcDecision(
            receipt_id=command.receipt_id,
            result=command.result,
            quantity=command.quantity,
            decided_by=normalize_text(command.decided_by),
            occurred_at=command.occurred_at,
        )
    )

    destination_state = BUCKET_STATE_BY_QC_RESULT[command.result]
    quarantine = _normalized_location(command.location_code)

    remaining = command.quantity
    for bucket_id, sku_id, source_location, available in _locked_pending_buckets(
        session, command.receipt_id
    ):
        if remaining == 0:
            break
        moved = min(remaining, available)
        session.execute(
            update(StockPosition)
            .where(StockPosition.id == bucket_id)
            .values(quantity=StockPosition.quantity - moved)
        )
        _add_to_bucket(
            session,
            sku_id=sku_id,
            receipt_id=command.receipt_id,
            qc_state=destination_state,
            location_code=quarantine if command.result == QC_FAIL else source_location,
            quantity=moved,
        )
        remaining -= moved

    if remaining:
        # 도메인 검증을 통과했다면 도달할 수 없다. 조용히 일부만 옮기면 보존
        # 불변식이 깨진 채로 커밋되므로 소리 내어 실패한다.
        raise ValueError(f"미검수 수량이 부족합니다: {remaining}개를 옮길 버킷이 없습니다")


def lock_ownership_status(session: Session, receipt_id: uuid.UUID) -> str | None:
    """입고의 귀속 상태를 잠그고 읽는다. None이면 그런 입고가 없다.

    잠그는 이유는 미검수 잔량과 같다. 두 담당자가 같은 미확인 입고를 동시에
    확인하면 버킷이 두 번 만들어져 수량이 두 배가 된다.
    """
    return session.execute(
        select(Receipt.ownership_status).where(Receipt.id == receipt_id).with_for_update()
    ).scalar_one_or_none()


@dataclass(frozen=True)
class NewOwnershipReview:
    """귀속 확인 시도 하나를 기록하는 데 필요한 값 묶음.

    seller_id·sku_id는 확인에 성공했을 때만 채워진다.
    """

    command: OwnershipReviewCommand
    seller_id: uuid.UUID | None
    sku_id: uuid.UUID | None


def _arrived_quantity(session: Session, receipt_id: uuid.UUID) -> int:
    """입고의 실도착 수량. 귀속 확인 뒤 만들 버킷의 수량 기준이 된다."""
    return session.execute(
        select(Receipt.arrived_quantity).where(Receipt.id == receipt_id)
    ).scalar_one()


def record_ownership_review(session: Session, new: NewOwnershipReview) -> None:
    """귀속 확인 시도를 기록하고, 확인됐으면 귀속을 확정한다 (AC-08).

    확인에 성공하면 세 가지가 한 트랜잭션에서 일어난다. 검토 이력이 쌓이고,
    receipt의 귀속이 확정되고, 재고 버킷이 생긴다. TX-04가 함께 판정하라고
    요구하는 범위다.

    생기는 버킷은 PENDING이다. 소유 확인은 "이 물건이 누구 것인지"를 답하고
    검수는 "쓸 수 있는지"를 답한다. NBR-16이 "소유권이 확인되어도 별도 QC가
    필요"하다고 명시하며, AC-08의 위반 조항도 확인 후 QC 없는 편입을 금지한다.

    확인하지 못한 검토는 이력만 쌓는다. 상태를 바꾸지 않는 것이 결과다 — 기한이
    지났다는 사실이 자동 폐기나 승인으로 해석되지 않는다(NBR-17, AC-11).
    """
    command = new.command
    session.add(
        OwnershipReview(
            receipt_id=command.receipt_id,
            outcome=command.outcome,
            reviewed_by=normalize_text(command.reviewed_by),
            note=normalize_text(command.note or "") or None,
            due_at=command.due_at,
            occurred_at=command.occurred_at,
        )
    )

    if command.outcome != REVIEW_CONFIRMED:
        return

    if new.seller_id is None or new.sku_id is None:
        # 도메인 검증을 통과했다면 도달할 수 없다. 조용히 넘기면 귀속이 없는
        # CONFIRMED 행이 만들어져 ck_receipt_confirmed_attribution에 걸린다.
        raise ValueError("확인된 검토에는 판매사와 품목이 필요합니다")

    session.execute(
        update(Receipt)
        .where(Receipt.id == command.receipt_id)
        .values(
            seller_id=new.seller_id,
            sku_id=new.sku_id,
            ownership_status=OWNERSHIP_CONFIRMED,
        )
    )
    session.execute(
        insert(StockPosition).values(
            sku_id=new.sku_id,
            receipt_id=command.receipt_id,
            qc_state=QC_STATE_PENDING,
            location_code=None,
            quantity=_arrived_quantity(session, command.receipt_id),
        )
    )

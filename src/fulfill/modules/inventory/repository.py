"""inventory 모듈의 영속화 (BC-03).

ORM 객체를 모듈 밖으로 내보내지 않는다. 호출자에게는 판정에 필요한 값만 담은
평범한 데이터 구조를 돌려준다.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from fulfill.modules.inventory.domain import (
    BUCKET_STATE_BY_QC_RESULT,
    OWNERSHIP_CONFIRMED,
    OWNERSHIP_UNKNOWN,
    QC_FAIL,
    QC_STATE_PASSED,
    QC_STATE_PENDING,
    REVIEW_CONFIRMED,
    AdviceCommand,
    HoldCommand,
    LiftHoldCommand,
    OwnershipReviewCommand,
    QcCommand,
    ReceiptCommand,
)
from fulfill.modules.inventory.models import (
    InboundAdvice,
    InventoryHold,
    OwnershipReview,
    QcDecision,
    Receipt,
    StockPosition,
)
from fulfill.modules.inventory.queries import active_hold_exists
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


@dataclass(frozen=True)
class AllocatableBucket:
    """신규 배정에 쓸 수 있는 버킷 하나와 그 가용량."""

    bucket_id: uuid.UUID
    # 버킷 수량이 아니라 아직 배정되지 않은 수량이다. 이미 다른 요청에 배정된
    # 몫을 뺀 값이므로, 이것을 그대로 쓰면 NBR-04의 중복 배정이 일어나지 않는다.
    available: int


def lock_allocatable_buckets(session: Session, sku_id: uuid.UUID) -> tuple[AllocatableBucket, ...]:
    """이 SKU에서 신규 배정 자격이 있는 버킷을 잠그고 돌려준다 (AC-07, AC-09).

    적격 조건이 `qc_state == PASSED` 하나뿐인 것은 24-2의 버킷 설계 효과다.
    미검수와 불합격은 각자 다른 버킷에 있어 따로 뺄 필요가 없고, 귀속 미확정
    물량은 버킷 자체가 없다(AC-08).

    위치를 조건에 넣지 않는다. NBR-12·AC-09가 "QC 합격 후 위치 미확정 상품은
    배정 가능하지만 피킹 불가"를 요구하므로, 여기서 위치를 걸러내면 그 요구를
    위반한다. 위치 관문은 피킹 판정의 일이다.

    사용 보류가 걸린 입고묶음은 제외한다. NBR-14의 "영향 가능 범위 사용 보류"이며,
    범위 단위가 입고묶음이므로 같은 SKU의 다른 입고묶음은 영향을 받지 않는다 —
    AC-10이 무관 재고 30개의 자동 차단을 금지한 그 구분이다.

    **제외는 신규 배정에만 걸린다.** 이미 붙어 있는 배정은 이 질의가 손대지
    않으므로 그대로 유효하다. AC-04는 조사 의심 단계에서 유효 배정을 무단 삭제
    하지 말라고 금지하며, 보류와 유효 배정은 공존한다.

    정렬은 위치가 확정된 버킷을 앞에 둔다. 업무 규칙이 아니라 동순위 사이의
    선택이다. 어느 실물을 어느 요청에 줄지는 어떤 문서도 정하지 않았지만, 위치
    미확정 버킷을 먼저 소진하면 "배정은 됐는데 피킹할 수 없는" 요청이 쌓인다.
    FIFO는 수요의 순서를 정하는 규칙이고 이 선택은 그것과 무관하다.

    잠그는 이유는 lock_pending_quantity와 같다. 가용량을 읽은 뒤 예약하기까지의
    틈에 다른 배정이 같은 버킷을 가져가면 두 배정의 합이 버킷 수량을 넘는다.
    CHECK 제약이 그 커밋을 거부하지만, 거기까지 가면 담당자는 업무 사유 대신
    서버 오류를 받는다.
    """
    rows = session.execute(
        select(
            StockPosition.id,
            StockPosition.quantity - StockPosition.allocated_quantity,
        )
        .where(
            StockPosition.sku_id == sku_id,
            StockPosition.qc_state == QC_STATE_PASSED,
            StockPosition.quantity > StockPosition.allocated_quantity,
            ~active_hold_exists(StockPosition.receipt_id),
        )
        .order_by(StockPosition.location_code.nulls_last(), StockPosition.id)
        .with_for_update()
    ).all()
    return tuple(AllocatableBucket(bucket_id=row[0], available=row[1]) for row in rows)


def reserve_in_bucket(session: Session, bucket_id: uuid.UUID, quantity: int) -> None:
    """버킷의 배정량을 늘린다 (AC-04).

    배정 사실의 원장은 allocation 테이블이고 이 컬럼은 그 합계다. 두 쓰기는 같은
    트랜잭션에 있어야 하며, 조정 계층이 그것을 보장한다.

    `allocated_quantity + quantity`를 SQL 식으로 쓴다. 파이썬에서 더해 넣으면 읽은
    값이 낡았을 수 있다. 여기서는 호출 전에 버킷을 잠그므로 둘의 결과가 같지만,
    잠금에 의존하지 않는 쪽이 안전하다.

    한도를 넘으면 ck_stock_allocated_within_quantity가 커밋을 거부한다. 도달하면
    잠금이나 배분 계산에 버그가 있다는 뜻이다.
    """
    session.execute(
        update(StockPosition)
        .where(StockPosition.id == bucket_id)
        .values(allocated_quantity=StockPosition.allocated_quantity + quantity)
    )


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


def release_in_bucket(session: Session, bucket_id: uuid.UUID, quantity: int) -> None:
    """버킷의 배정량을 줄인다 (AC-03, CB-03).

    `reserve_in_bucket`의 역방향이다. 이것이 없으면 정당하게 풀린 수량이 버킷에
    묶인 채 남아 다시 배정되지 않는다 — CB-03이 "정당 해제된 물량은 그 시점의
    선순위 미충족 구간부터 고려한다"고 요구하는 재배정이 일어나지 않는다.

    음수로 내려가면 ck_stock_allocated_within_quantity가 커밋을 거부한다. 도달하면
    해제 계산이 배정량보다 많이 풀었다는 뜻이다.
    """
    session.execute(
        update(StockPosition)
        .where(StockPosition.id == bucket_id)
        .values(allocated_quantity=StockPosition.allocated_quantity - quantity)
    )


# 활성 보류 부분 유니크 인덱스의 이름. 중복 보류 판정이 이 인덱스에 의존한다.
ACTIVE_HOLD_CONSTRAINT = "uq_hold_active_per_receipt"


def insert_hold(session: Session, command: HoldCommand) -> uuid.UUID | None:
    """사용 보류를 걸고 보류 id를 돌려준다. None이면 이미 보류 중이다 (AC-10).

    조회해서 없으면 넣는 방식을 쓰지 않는다. 확인과 삽입 사이에 다른 담당자가
    같은 입고묶음을 보류하면 활성 보류가 둘이 되고, 그러면 해제 한 번으로 풀리지
    않는 재고가 생긴다. 24-1의 멱등 경로에서 본 것과 같은 틈이다.

    대신 부분 유니크 인덱스에 판정을 맡긴다. 충돌하면 아무것도 넣지 않고
    RETURNING이 비어 돌아오므로, 왕복 한 번으로 "넣었다"와 "이미 있다"가 갈린다.
    `index_where`가 인덱스의 조건과 같아야 PostgreSQL이 이 인덱스를 충돌 대상으로
    인식한다.

    중복 보류를 오류로 올리지 않는다. 같은 재고를 두 번 묶으려는 시도는 업무상
    사고가 아니라 이미 달성된 상태이며, 판정 어휘를 고르는 것은 조정 계층의 일이다.
    """
    return session.execute(
        insert(InventoryHold)
        .values(
            receipt_id=command.receipt_id,
            reason=command.reason,
            note=normalize_text(command.note or "") or None,
            held_by=normalize_text(command.held_by),
            occurred_at=command.occurred_at,
        )
        .on_conflict_do_nothing(
            index_elements=["receipt_id"],
            index_where=InventoryHold.lifted_at.is_(None),
        )
        .returning(InventoryHold.id)
    ).scalar_one_or_none()


def lift_hold(session: Session, command: LiftHoldCommand) -> uuid.UUID | None:
    """사용 보류를 풀고 보류 id를 돌려준다. None이면 활성 보류가 없었다 (AC-10).

    활성 조건을 WHERE에 둔다. 그러면 "활성인 것만 해제한다"가 조회 없이 성립하고,
    이미 풀린 보류에 대한 요청은 0행을 갱신하고 끝난다. 먼저 조회해 판정하면 그
    사이에 다른 담당자가 해제할 수 있고, 두 해제가 모두 성공한 것처럼 보이면서
    나중 것이 앞선 해제 주체와 시각을 덮어쓴다.

    해제 기록 시각은 DB 시계로 찍는다. 발생 시각은 담당자가 말한 때이고 이쪽은
    시스템이 그 말을 받은 때이므로, 둘을 같은 출처에서 가져오면 구분이 사라진다.
    """
    return session.execute(
        update(InventoryHold)
        .where(
            InventoryHold.receipt_id == command.receipt_id,
            InventoryHold.lifted_at.is_(None),
        )
        .values(
            lifted_at=command.occurred_at,
            lifted_recorded_at=func.now(),
            lifted_by=normalize_text(command.lifted_by),
            lifted_note=normalize_text(command.note),
        )
        .returning(InventoryHold.id)
    ).scalar_one_or_none()

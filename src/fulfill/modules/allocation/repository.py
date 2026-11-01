"""allocation 모듈의 영속화 (BC-04).

ORM 객체를 모듈 밖으로 내보내지 않는다. 호출자에게는 판정에 필요한 값만 담은
평범한 데이터 구조를 돌려준다.
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from fulfill.modules.allocation.domain import (
    ActiveAllocation,
    PlannedAllocation,
    PlannedRelease,
)
from fulfill.modules.allocation.models import Allocation, AllocationRelease


def insert_allocations(
    session: Session, sku_id: uuid.UUID, plan: Iterable[PlannedAllocation]
) -> None:
    """배분 계획을 배정 사실로 기록한다 (AC-03, AC-04).

    버킷의 배정량을 늘리는 일은 여기서 하지 않는다. `stock_position`은 BC-03의
    소유이므로 inventory가 공개한 함수를 통해야 하고, 두 쓰기를 한 트랜잭션에
    묶는 것은 조정 계층의 책임이다(ADR23 §3).

    행을 합치지 않는다. 같은 (구간, 버킷) 쌍이 이미 있어도 새 행을 만든다. 합치면
    각 배정의 시각이 사라져 AC-36이 요구하는 "SKU별 배정" 이력을 잃는다.

    sku_id를 함께 넣는다. 계획에는 없는 값이지만 모든 행이 같은 SKU에 속하므로
    호출자가 한 번 넘긴다. 이 컬럼이 있어야 배정 합계를 demand_segment 조인 없이
    읽을 수 있다(모델의 docstring 참조).
    """
    session.add_all(
        Allocation(
            demand_segment_id=item.segment_id,
            stock_position_id=item.bucket_id,
            sku_id=sku_id,
            quantity=item.quantity,
        )
        for item in plan
    )


def lock_active_allocations(
    session: Session, segment_id: uuid.UUID
) -> tuple[ActiveAllocation, ...]:
    """이 수요 구간의 유효 배정을 잠그고 최신 순으로 돌려준다 (CB-03).

    최신 순인 것은 동순위 사이의 선택이다. 어느 배정을 먼저 풀지는 어떤 문서도
    정하지 않았지만, 감량이 최신 구간부터인 것과 방향을 맞추면 오래전에 잡아 둔
    배정이 더 오래 안정적으로 남는다. 순서가 결정론적이어야 하는 것은 별개의
    이유다 — 같은 입력이 같은 결과를 내야 테스트가 성립한다.

    잠그는 이유는 배정 쪽과 같다. 유효 수량을 읽은 뒤 해제를 기록하기까지의 틈에
    다른 트랜잭션이 같은 배정을 풀면 합이 배정량을 넘는다. CHECK 제약이 그 커밋을
    거부하지만, 거기까지 가면 담당자는 업무 사유 대신 서버 오류를 받는다.
    """
    rows = session.execute(
        select(
            Allocation.id,
            Allocation.stock_position_id,
            Allocation.quantity - Allocation.released_quantity,
        )
        .where(
            Allocation.demand_segment_id == segment_id,
            Allocation.quantity > Allocation.released_quantity,
        )
        .order_by(Allocation.allocated_at.desc(), Allocation.id.desc())
        .with_for_update()
    ).all()
    return tuple(
        ActiveAllocation(allocation_id=row[0], bucket_id=row[1], active=row[2]) for row in rows
    )


def apply_releases(session: Session, plan: Iterable[PlannedRelease], reason: str) -> None:
    """해제 계획을 사실로 기록하고 배정의 풀린 수량을 늘린다 (AC-03, CB-03).

    버킷의 배정량을 되돌리는 일은 여기서 하지 않는다. `stock_position`은 BC-03의
    소유이므로 inventory가 공개한 함수를 통해야 하고, 두 쓰기를 한 트랜잭션에
    묶는 것은 조정 계층의 책임이다(ADR23 §3).

    사유를 인자로 받는다. 24-3에서 올 수 있는 값은 수요 감소 하나뿐이지만, 사후
    불량·손실 확정(D-09)과 요청 취소(POL-005)도 정당한 해제 사유이므로 이 함수가
    사유를 고르지 않는다.
    """
    for item in plan:
        session.add(
            AllocationRelease(
                allocation_id=item.allocation_id,
                quantity=item.quantity,
                reason=reason,
            )
        )
        session.execute(
            update(Allocation)
            .where(Allocation.id == item.allocation_id)
            .values(released_quantity=Allocation.released_quantity + item.quantity)
        )

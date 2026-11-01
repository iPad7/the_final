"""allocation 모듈의 영속화 (BC-04).

ORM 객체를 모듈 밖으로 내보내지 않는다. 호출자에게는 판정에 필요한 값만 담은
평범한 데이터 구조를 돌려준다.
"""

import uuid
from collections.abc import Iterable

from sqlalchemy.orm import Session

from fulfill.modules.allocation.domain import PlannedAllocation
from fulfill.modules.allocation.models import Allocation


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

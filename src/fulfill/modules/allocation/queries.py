"""BC-04가 공개하는 조회 입구."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fulfill.modules.allocation.models import Allocation


def sum_allocated_by_segment(session: Session, sku_id: uuid.UUID) -> dict[uuid.UUID, int]:
    """이 SKU의 수요 구간별 유효 배정 합계 (AC-03, AC-04).

    배정이 없는 구간은 결과에 없다. 호출자는 그것을 0으로 읽는다.

    `demand_segment`를 조인하지 않는 것이 핵심이다. 조인하면 BC-04가 BC-02의 ORM을
    직접 보게 되어 모듈 경계가 깨진다(ADR23 §3). allocation이 sku_id를 중복해
    들고 있는 이유가 바로 이 한 줄이다.

    한 구간에 여러 행이 쌓이는 것이 정상이므로(분할 입고, 2차 배정) 합계를
    구한다. 행 수를 세면 안 된다.
    """
    rows = session.execute(
        select(Allocation.demand_segment_id, func.sum(Allocation.quantity))
        .where(Allocation.sku_id == sku_id)
        .group_by(Allocation.demand_segment_id)
    ).all()
    return {row[0]: int(row[1]) for row in rows}

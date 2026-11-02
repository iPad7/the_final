"""BC-04가 공개하는 조회 입구."""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

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

    풀린 수량을 뺀다. 해제 행은 이력으로 남고 배정 행도 지워지지 않으므로, 빼지
    않으면 수요 감소로 풀어 준 수량이 여전히 배정된 것으로 보인다. 그러면 그
    구간은 영구히 "충족됨"이 되어 재배정 대상에서 빠진다.
    """
    rows = session.execute(
        select(
            Allocation.demand_segment_id,
            func.sum(Allocation.quantity - Allocation.released_quantity),
        )
        .where(Allocation.sku_id == sku_id)
        .group_by(Allocation.demand_segment_id)
    ).all()
    return {row[0]: int(row[1]) for row in rows}


@dataclass(frozen=True)
class AllocatedPosition:
    """유효 배정이 걸려 있는 재고 버킷 하나와 그 수량."""

    bucket_id: uuid.UUID
    quantity: int


def list_allocated_positions(
    session: Session, segment_ids: Iterable[uuid.UUID]
) -> tuple[AllocatedPosition, ...]:
    """이 수요 구간들의 유효 배정을 재고 버킷 단위로 집계한다 (AC-05, AC-09).

    피킹 준비 판정이 쓸 입구다. 관문이 물어야 하는 것은 "이 요청의 배정이 어느
    버킷에 걸려 있는가"이고, 그 버킷의 위치와 보류 여부는 BC-03이 답한다.

    버킷으로 묶는 이유는 판정 축이 버킷에 붙어 있기 때문이다. 위치는 버킷의
    컬럼이고 보류는 버킷이 속한 입고묶음에 걸리므로, 구간별로 두면 같은 버킷을
    여러 번 조회하게 된다.

    `demand_segment`를 조인하지 않는다. FK 컬럼만 IN으로 좁히므로 BC-02의 ORM을
    보지 않는다. `sum_allocated_by_segment`가 sku_id를 중복해 들고 있는 것과 같은
    이유다(ADR23 §3).

    풀린 수량을 뺀 뒤 0인 버킷은 제외한다. 전량 해제된 배정이 남긴 버킷은 이미 이
    요청의 재고가 아니므로 "배정이 걸린 버킷"이라는 이름에 맞지 않는다.

    **이 제외는 현재 호출자의 판정을 바꾸지 않는다.** 관문이 수량을 합산하므로 0인
    행은 합계에 영향이 없다. 질의의 계약을 분명히 해 두는 쪽이고, 버킷 수를 세거나
    유무만 보는 호출자가 생기면 그때 의미를 갖는다. 계약을 지키는지는 테스트로
    따로 고정한다.
    """
    effective = func.sum(Allocation.quantity - Allocation.released_quantity)
    rows = session.execute(
        select(Allocation.stock_position_id, effective)
        .where(Allocation.demand_segment_id.in_(list(segment_ids)))
        .group_by(Allocation.stock_position_id)
        .having(effective > 0)
    ).all()
    return tuple(AllocatedPosition(bucket_id=row[0], quantity=int(row[1])) for row in rows)

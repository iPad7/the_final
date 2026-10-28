"""BC-04가 공개하는 입구: FIFO 순번 발급.

다른 모듈은 guard 테이블을 직접 건드리지 않고 이 함수들을 호출한다. 그래서
잠금 규칙이 한 곳에만 존재한다.
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from fulfill.modules.allocation.models import SkuDemandGuard


def issue_demand_seq(session: Session, sku_id: uuid.UUID) -> int:
    """이 SKU의 다음 FIFO 순번을 확보한다.

    그 순번이 붙을 수요를 함께 기록하는 트랜잭션 안에서 실행해야 한다. 그렇지
    않으면 발급된 순번이 쓰이지 않은 채 남을 수 있다.
    """
    # 재고가 아직 없는 SKU에도 guard는 존재해야 한다(ADR23 §4). ON CONFLICT DO
    # NOTHING이 최초 삽입 경쟁을 흡수한다. 패자는 유니크 인덱스에서 대기하다가
    # 행이 이미 있는 것을 확인하고 넘어간다.
    session.execute(
        insert(SkuDemandGuard)
        .values(sku_id=sku_id, next_demand_seq=1)
        .on_conflict_do_nothing(index_elements=["sku_id"])
    )

    # 경쟁 트랜잭션은 여기서 줄을 선다. 그래서 발급 순서가 커밋 순서가 된다.
    seq = session.execute(
        select(SkuDemandGuard.next_demand_seq)
        .where(SkuDemandGuard.sku_id == sku_id)
        .with_for_update()
    ).scalar_one()

    session.execute(
        update(SkuDemandGuard)
        .where(SkuDemandGuard.sku_id == sku_id)
        .values(next_demand_seq=seq + 1)
    )
    return seq


def issue_demand_seqs(session: Session, sku_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, int]:
    """SKU마다 순번 하나를 확보한다. 잠금은 정해진 순서로 취득한다.

    정렬이 중요하다. 두 다품목 요청이 guard를 반대 순서로 잡으면 교착한다.
    합의된 순서 하나가 그 사이클을 없앤다(ADR23 §4).
    """
    return {sku_id: issue_demand_seq(session, sku_id) for sku_id in sorted(set(sku_ids))}

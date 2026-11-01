"""BC-04가 공개하는 입구: FIFO 순번 발급과 큐 잠금.

다른 모듈은 guard 테이블을 직접 건드리지 않고 이 함수들을 호출한다. 그래서
잠금 규칙이 한 곳에만 존재한다.

두 입구가 같은 행을 잠근다. 수요가 생길 때(순번 발급)와 배정을 판정할 때(큐
잠금) 모두 같은 판매사+SKU의 순서를 건드리므로, 같은 잠금 지점을 지나야 TX-03이
요구하는 "순서 경합은 일관된 판정"이 성립한다.
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from fulfill.modules.allocation.models import SkuDemandGuard


def issue_demand_seq(session: Session, sku_id: uuid.UUID) -> int:
    """이 SKU의 다음 FIFO 순번을 확보한다.

    그 순번이 붙을 수요를 함께 기록하는 트랜잭션 안에서 실행해야 한다. 그렇지
    않으면 발급된 순번이 쓰이지 않은 채 남을 수 있다.

    한 문장으로 처리한다. 삽입·조회·갱신 세 번을 왕복하던 것을 줄인 것이며, 줄인
    이유는 왕복 수 자체보다 **잠금을 잡기 전 틈이 없어지는 것**에 있다.

    - 행이 없으면 2를 넣고 `RETURNING next_demand_seq - 1`이 1을 돌려준다.
    - 행이 있으면 기존 값에 1을 더하고 더하기 전 값을 돌려준다.
    - `DO UPDATE`는 행 배타 잠금을 잡는다. 그래서 경쟁 트랜잭션은 여기서 줄을
      서고 발급 순서가 커밋 순서가 된다. 이 주장은 실PG 경합 테스트가 확인한다.

    PostgreSQL sequence로는 이 일을 할 수 없다. 이유는 모델의 docstring에 있다.
    """
    # set_에서 테이블 컬럼을 참조하면 기존 행의 값을 뜻한다(excluded가 아니다).
    # 최초 삽입 경쟁은 PostgreSQL이 흡수한다. 상대가 아직 커밋하지 않았다면 이
    # 문장이 대기한 뒤 갱신으로 넘어간다.
    return session.execute(
        insert(SkuDemandGuard)
        .values(sku_id=sku_id, next_demand_seq=2)
        .on_conflict_do_update(
            index_elements=["sku_id"],
            set_={"next_demand_seq": SkuDemandGuard.next_demand_seq + 1},
        )
        .returning(SkuDemandGuard.next_demand_seq - 1)
    ).scalar_one()


def lock_sku_queue(session: Session, sku_id: uuid.UUID) -> None:
    """이 SKU의 수요 큐를 잠근다. 순번은 소비하지 않는다.

    배정이 쓰는 입구다. 배정은 새 수요를 만들지 않으므로 순번을 받을 이유가 없고,
    받으면 쓰이지 않은 번호가 결번으로 남는다.

    잠그는 이유는 ADR23 §4가 정한 것이다. 같은 판매사+SKU의 수요 순서·적격
    물량·기존 배정을 **하나의 트랜잭션 안에서** 평가해야 하며, 그 평가가 서로
    끼어들면 두 트랜잭션이 같은 적격 수량을 각자 자기 몫으로 계산한다.

    재고가 아직 없는 SKU에도 guard는 존재해야 한다(ADR23 §4). 그래서 없으면
    만든다. `DO NOTHING`을 쓰는 것은 여기서 아무 값도 바꿀 필요가 없기 때문이며,
    불필요한 갱신은 가장 경합이 심한 행에 새 행 버전을 남긴다.
    """
    session.execute(
        insert(SkuDemandGuard)
        .values(sku_id=sku_id, next_demand_seq=1)
        .on_conflict_do_nothing(index_elements=["sku_id"])
    )
    # 반환값을 쓰지 않는다. 필요한 것은 이 문장이 거는 잠금이다. scalar_one은
    # 행이 없으면 예외를 내므로, 위 삽입이 제 일을 했는지도 함께 확인된다.
    session.execute(
        select(SkuDemandGuard.sku_id).where(SkuDemandGuard.sku_id == sku_id).with_for_update()
    ).scalar_one()


def issue_demand_seqs(session: Session, sku_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, int]:
    """SKU마다 순번 하나를 확보한다. 잠금은 정해진 순서로 취득한다.

    정렬이 중요하다. 두 다품목 요청이 guard를 반대 순서로 잡으면 교착한다.
    합의된 순서 하나가 그 사이클을 없앤다(ADR23 §4).
    """
    return {sku_id: issue_demand_seq(session, sku_id) for sku_id in sorted(set(sku_ids))}

"""피킹 준비 판정의 조정 계층 (AC-05, AC-09).

24-3의 `summarize_request_allocation`은 "전량 배정됐는가"까지만 답했고, 그 값을
피킹 허가로 읽으면 AC-09 위반이라고 경고를 달아 두었다. 이 모듈이 그 경고를
해소한다.

관문은 세 조건을 함께 본다. NBR-07의 전량 확보, NBR-12의 위치 확인, NBR-14의
사용 보류 없음이며, AC-05가 "모든 SKU 전량 확보와 위치/사용 제한 확인 전 정상
작업 착수를 허용하지 않는다"고 묶어 둔 바로 그 셋이다.

**판정만 한다.** 작업 수락·수집 실행은 AC-13~16의 일이고 24단계 범위 밖이다.
여기서 ready가 참이어도 작업이 시작되지는 않는다.

세 모듈의 조회를 합친다. 라인과 수요 구간은 BC-02, 배정이 걸린 버킷은 BC-04,
버킷의 위치와 보류는 BC-03이 가진 사실이고 어느 하나도 나머지를 볼 수 없다.
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from fulfill.modules.allocation.queries import (
    AllocatedPosition,
    list_allocated_positions,
    sum_allocated_by_segment,
)
from fulfill.modules.inventory.queries import (
    BucketPickability,
    describe_bucket_pickability,
    find_sku_codes_by_ids,
)
from fulfill.modules.requests.queries import summarize_request_demand


class PickingBlocker(StrEnum):
    """피킹 착수를 막는 사유.

    세 사유를 구별하는 것이 이 열거의 목적이다. 해결 방법이 전혀 다르다 — 재고를
    더 받아야 하는지, 선반 위치를 확인해야 하는지, 조사를 끝내야 하는지. 불리언
    하나로 답하면 담당자가 무엇을 해야 할지 알 수 없고, AC-05가 요구하는 "작업
    관문 판단"의 근거도 되지 못한다.
    """

    # 필요 수량만큼 배정되지 않았다. 부분 배정은 유지되지만 작업은 못 한다(NBR-07).
    UNALLOCATED = "UNALLOCATED"
    # 배정은 됐지만 그 재고의 실제 피킹 위치가 확인되지 않았다(NBR-12, AC-09).
    LOCATION_UNCONFIRMED = "LOCATION_UNCONFIRMED"
    # 배정된 재고의 입고묶음이 사용 보류 중이다(NBR-14, AC-04).
    HELD = "HELD"


@dataclass(frozen=True)
class LineReadiness:
    """요청 라인 하나의 관문 판정과 그 근거 수량.

    세 수량이 서로 배타적이지 않다. 위치 미확정이면서 보류인 재고는 두 수량에 모두
    들어간다. 더해서 쓰면 중복 계산이 되므로 각각 "이 사유에 걸린 양"으로만 읽는다
    — CB-04가 onHold+allocated+picked의 단순 합산을 경고한 것과 같은 성격이다.
    """

    sku_code: str
    required_quantity: int
    allocated_quantity: int
    # 아직 확보되지 않은 수량.
    unallocated_quantity: int
    # 배정됐지만 피킹 위치가 미확정인 수량.
    location_unconfirmed_quantity: int
    # 배정됐지만 사용 보류 중인 수량.
    held_quantity: int
    blockers: tuple[PickingBlocker, ...]

    @property
    def ready(self) -> bool:
        """이 라인만 보면 작업 가능한가."""
        return not self.blockers


@dataclass(frozen=True)
class PickingReadiness:
    """요청 하나의 피킹 준비 판정 (AC-05)."""

    request_id: uuid.UUID
    lines: tuple[LineReadiness, ...]
    # 라인들의 막힘 사유 합집합. 요청 단위로 무엇을 해결해야 하는지 보여 준다.
    blockers: tuple[PickingBlocker, ...]

    @property
    def ready(self) -> bool:
        """정상 피킹에 착수해도 되는가.

        **부분 출고를 허용하지 않는다는 뜻이 여기 있다.** 라인 하나라도 막히면
        요청 전체가 막힌다. 확보된 SKU만 먼저 담아 보내는 것이 AC-05가 금지한
        일반 부분 출고다(NBR-07).
        """
        return not self.blockers


def judge_line_blockers(
    required_quantity: int,
    allocated_quantity: int,
    location_unconfirmed_quantity: int,
    held_quantity: int,
) -> tuple[PickingBlocker, ...]:
    """수량들로부터 막힘 사유를 가린다 (AC-05, AC-09).

    DB를 모르는 순수 판정이다. 관문의 규칙을 여기 모아 두면 조회 없이 전수로
    고정할 수 있고, 통합 테스트는 "이 규칙이 올바른 입력을 받는가"만 보면 된다.
    24-3에서 `plan_allocations`를 분리한 것과 같은 이유다.

    사유 순서를 고정한다. 담당자가 보는 목록이 호출마다 뒤바뀌면 같은 상태가
    다르게 보인다. 순서는 해결 순서이기도 하다 — 확보되지 않은 재고에는 위치도
    없다.

    초과 배정은 막힘이 아니다. `allocated > required`는 수요 감소 직후처럼 일시적
    으로 생길 수 있고 작업을 막을 근거는 아니다.
    """
    blockers: list[PickingBlocker] = []

    if allocated_quantity < required_quantity:
        blockers.append(PickingBlocker.UNALLOCATED)
    if location_unconfirmed_quantity > 0:
        blockers.append(PickingBlocker.LOCATION_UNCONFIRMED)
    if held_quantity > 0:
        blockers.append(PickingBlocker.HELD)

    return tuple(blockers)


@dataclass(frozen=True)
class _LineFacts:
    """한 라인에서 모은 수량과 그 배정이 걸린 버킷들."""

    sku_id: uuid.UUID
    required_quantity: int
    allocated_quantity: int
    positions: tuple[AllocatedPosition, ...]


def evaluate_picking_readiness(session: Session, request_id: uuid.UUID) -> PickingReadiness | None:
    """요청의 피킹 준비 여부를 판정한다. None이면 그런 요청이 없다 (AC-05, AC-09).

    버킷 설명을 모든 라인의 버킷을 모아 한 번에 조회한다. 라인마다 따로 물으면
    질의가 라인 수만큼 늘고, 같은 입고묶음의 버킷이 두 라인에 걸릴 때 같은 행을
    여러 번 읽는다.

    잠그지 않는다. 현황 조회이며 이 값을 근거로 쓰기를 결정하는 경로가 없다. 실제
    작업 착수는 24단계 범위 밖이고, 그 지점에서는 다시 잠그고 판정해야 한다 —
    판정과 착수 사이에 보류가 걸리거나 배정이 풀릴 수 있다.
    """
    lines = summarize_request_demand(session, request_id)
    if lines is None:
        return None

    facts: list[_LineFacts] = []
    for line in lines:
        allocated_by_segment = sum_allocated_by_segment(session, line.sku_id)
        segment_ids = [segment.segment_id for segment in line.segments]
        facts.append(
            _LineFacts(
                sku_id=line.sku_id,
                required_quantity=line.quantity,
                # 라인 수량이 아니라 구간별 배정의 합이다. 배정은 구간 단위로
                # 일어나므로 이 합만이 실제 배정량이다.
                allocated_quantity=sum(
                    allocated_by_segment.get(segment_id, 0) for segment_id in segment_ids
                ),
                positions=list_allocated_positions(session, segment_ids),
            )
        )

    pickability = describe_bucket_pickability(
        session,
        {position.bucket_id for fact in facts for position in fact.positions},
    )
    codes = find_sku_codes_by_ids(session, [fact.sku_id for fact in facts])

    summaries: list[LineReadiness] = []
    for fact in facts:
        # 조회 사이에 버킷이 사라졌다면 설명이 없다. 그 경우를 "확인되지 않았고
        # 보류일 수 있다"로 읽어, 모르는 상태가 작업 허가로 바뀌지 않게 한다.
        unknown = BucketPickability(location_confirmed=False, held=True)
        unconfirmed = sum(
            position.quantity
            for position in fact.positions
            if not pickability.get(position.bucket_id, unknown).location_confirmed
        )
        held = sum(
            position.quantity
            for position in fact.positions
            if pickability.get(position.bucket_id, unknown).held
        )
        summaries.append(
            LineReadiness(
                sku_code=codes[fact.sku_id],
                required_quantity=fact.required_quantity,
                allocated_quantity=fact.allocated_quantity,
                unallocated_quantity=max(fact.required_quantity - fact.allocated_quantity, 0),
                location_unconfirmed_quantity=unconfirmed,
                held_quantity=held,
                blockers=judge_line_blockers(
                    required_quantity=fact.required_quantity,
                    allocated_quantity=fact.allocated_quantity,
                    location_unconfirmed_quantity=unconfirmed,
                    held_quantity=held,
                ),
            )
        )

    # 합집합이지만 열거 순서를 따른다. 사유 목록이 라인 순서에 따라 뒤바뀌면 같은
    # 상태가 다르게 보인다.
    raised = {blocker for line in summaries for blocker in line.blockers}
    return PickingReadiness(
        request_id=request_id,
        lines=tuple(summaries),
        blockers=tuple(blocker for blocker in PickingBlocker if blocker in raised),
    )

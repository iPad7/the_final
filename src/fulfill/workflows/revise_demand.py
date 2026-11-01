"""수량 변경과 정당 해제의 조정 계층 (TX-02, CB-03).

TX-02가 "RequestRevision + 모든 SKU DemandSegment 및 정당한 배정 해제"를 함께
판정하라고 정한 범위다. 여러 라인이 바뀌는 명령에서 일부만 적용된 상태가 유효
최신 버전으로 보이면 안 되며, 그것은 트랜잭션 하나가 보장한다.

이 모듈은 어느 모듈의 ORM도 직접 보지 않는다.
"""

import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy.orm import Session

from fulfill.modules.allocation.domain import RELEASE_DEMAND_DECREASE, plan_releases
from fulfill.modules.allocation.guard import issue_demand_seq, lock_sku_queue
from fulfill.modules.allocation.repository import apply_releases, lock_active_allocations
from fulfill.modules.inventory.queries import find_sku_ids_by_codes
from fulfill.modules.inventory.repository import release_in_bucket
from fulfill.modules.requests.domain import ReviseCommand, plan_demand_reduction, validate_revision
from fulfill.modules.requests.repository import (
    LineState,
    QuantityChange,
    find_existing,
    insert_increase_segment,
    insert_revisions,
    lock_lines_by_sku,
    reduce_segment,
    update_line_quantity,
)
from fulfill.modules.seller_contracts.queries import find_seller_id_by_code
from fulfill.shared.validation import ValidationProblem, normalize_text
from fulfill.workflows.allocate import allocate_sku


class RevisionOutcome(StrEnum):
    """수량 변경 명령의 판정 결과.

    UNCHANGED가 있는 것이 중요하다. 명령이 변경 후 **총 수량**을 담으므로, 같은
    명령을 두 번 받으면 두 번째는 차이가 0이고 아무것도 바뀌지 않는다. 증감분을
    받는 설계였다면 재전송이 수량을 두 배로 만들었을 것이다.
    """

    # 하나 이상의 라인 수량이 바뀌었다.
    APPLIED = "APPLIED"
    # 모든 라인이 이미 그 수량이었다. 재전송이거나 바뀔 것이 없다.
    UNCHANGED = "UNCHANGED"
    # 명령이나 대상이 유효하지 않다.
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class RevisionResult:
    """수량 변경 판정과 그 근거.

    풀린 수량과 재배정된 수량을 나눠 담는다. AC-03이 "구간별 수요/배정/감소
    사유"를 업무 근거로 요구하고, 둘을 합치면 "다른 요청에서 빼앗은 것인지"와
    "줄인 만큼 풀린 것인지"를 구분할 수 없게 된다.
    """

    outcome: RevisionOutcome
    request_id: uuid.UUID | None = None
    # 수요가 줄어 정당하게 풀린 수량.
    released_quantity: int = 0
    # 풀린 물량이 같은 트랜잭션에서 선순위로 다시 배정된 수량 (CB-03).
    reallocated_quantity: int = 0
    problems: tuple[ValidationProblem, ...] = field(default_factory=tuple)


def _rejected(field_name: str, reason: str) -> RevisionResult:
    """사유 하나로 거절한다."""
    return RevisionResult(
        RevisionOutcome.REJECTED, problems=(ValidationProblem(field_name, reason),)
    )


def _reduce_line(session: Session, state: LineState, shortfall: int) -> int:
    """라인의 수요를 줄이고 초과 배정분을 푼다. 푼 수량을 돌려준다 (AC-03, CB-03).

    구간은 최신부터 줄어든다(D-01). 그러고 나면 줄어든 구간의 유효 배정이 남은
    수량을 넘을 수 있고, **넘는 만큼만** 푼다. 그것이 CB-03의 "정당하게 풀린
    예약만 해제 가능"이다. 더 풀면 줄이지 않은 수요의 배정을 빼앗는 것이 되고,
    그것은 NBR-04가 금지한 유효 배정 강제 회수다.

    세 쓰기가 한 묶음으로 일어난다. 해제 사실을 남기고(allocation_release),
    배정의 풀린 수량을 늘리고(allocation), 버킷의 배정량을 되돌린다
    (stock_position). 마지막 하나가 빠지면 풀린 수량이 버킷에 묶인 채 남아 다시
    배정되지 않는다.
    """
    quantities = {segment.segment_id: segment.quantity for segment in state.segments}
    reductions = plan_demand_reduction(state.segments, shortfall)
    if sum(item.quantity for item in reductions) != shortfall:
        # 구간 수량의 합은 라인 수량과 같아야 하므로 도달할 수 없다. 조용히 일부만
        # 줄이면 라인 수량과 구간 합이 어긋난 채 커밋된다.
        raise ValueError(f"줄일 수요 구간이 부족합니다: {shortfall}개를 줄일 수 없습니다")

    released = 0
    for reduction in reductions:
        reduce_segment(session, reduction.segment_id, reduction.quantity)
        remaining = quantities[reduction.segment_id] - reduction.quantity

        active = lock_active_allocations(session, reduction.segment_id)
        excess = sum(item.active for item in active) - remaining
        if excess <= 0:
            # 이 구간은 배정이 남은 수량 안에 들어간다. 풀 것이 없다.
            continue

        plan = plan_releases(active, excess)
        apply_releases(session, plan, RELEASE_DEMAND_DECREASE)
        for item in plan:
            release_in_bucket(session, item.bucket_id, item.quantity)
        released += sum(item.quantity for item in plan)

    return released


def revise_demand(session: Session, command: ReviseCommand) -> RevisionResult:
    """수량 변경을 접수한다 (AC-03).

    잠금 순서가 배정과 같다 — **guard → 라인**. 여러 SKU가 걸리면 guard를 정렬
    순서로 잡는다. 24-1의 다중 라인 접수와 같은 규칙이며, 순서를 뒤집으면 두
    변경이 서로가 쥔 행을 요구해 교착한다.

    재배정을 **풀린 수량이 있을 때만** 연쇄한다. 판단이 들어간 지점이다.

    - 해제는 CB-03이 "정당 해제된 물량은 그 시점의 동일 seller+SKU 선순위 미충족
      구간부터 고려한다"고 명시하므로 같은 트랜잭션에서 끝내야 한다. 미루면 풀린
      물량이 미배정으로 떠 있는 중간 상태가 외부에 보인다(TX-02).
    - 증량은 새 수요다. 새 요청이 접수된 것과 성격이 같고, AC-03은 "신규 가용
      재고가 입고되어 FIFO 배정하면"을 전제로 한다. 그래서 배정 실행을 기다린다 —
      검수 합격이 배정을 연쇄하지 않는 것과 같은 선이다.
    """
    problems = validate_revision(command)
    if problems:
        return RevisionResult(RevisionOutcome.REJECTED, problems=problems)

    seller_id = find_seller_id_by_code(session, normalize_text(command.seller_code))
    if seller_id is None:
        return _rejected("seller_code", "등록되지 않은 판매사입니다")

    existing = find_existing(session, seller_id, normalize_text(command.external_request_id))
    if existing is None:
        return _rejected("external_request_id", "등록되지 않은 출고 요청입니다")

    codes = [normalize_text(line.sku_code) for line in command.lines]
    sku_ids = find_sku_ids_by_codes(session, seller_id, codes)
    unknown = tuple(
        ValidationProblem(f"lines[{index}].sku_code", "이 판매사에 등록되지 않은 SKU입니다")
        for index, code in enumerate(codes)
        if code not in sku_ids
    )
    if unknown:
        return RevisionResult(RevisionOutcome.REJECTED, request_id=existing.id, problems=unknown)

    targets = [sku_ids[code] for code in codes]
    for sku_id in sorted(set(targets)):
        lock_sku_queue(session, sku_id)

    lines = lock_lines_by_sku(session, existing.id, targets)
    # 요청에 없는 품목은 수량 변경 대상이 아니다. 라인을 새로 만들어 주는 것은
    # SKU 구성 변경이고 NBR-06이 취소 후 신규 요청으로 처리하라고 정했다.
    absent = tuple(
        ValidationProblem(f"lines[{index}].sku_code", "이 요청에 없는 품목입니다")
        for index, code in enumerate(codes)
        if sku_ids[code] not in lines
    )
    if absent:
        return RevisionResult(RevisionOutcome.REJECTED, request_id=existing.id, problems=absent)

    changes: list[QuantityChange] = []
    released = 0
    released_skus: set[uuid.UUID] = set()

    for line_command, code in zip(command.lines, codes, strict=True):
        state = lines[sku_ids[code]]
        if line_command.quantity == state.quantity:
            # 바뀐 것이 없다. 변경 사실을 남기지 않는다 —
            # ck_revision_quantity_changed가 그런 행을 거부한다.
            continue

        if line_command.quantity > state.quantity:
            # 증량분은 변경 시점의 새 순번을 받는다(NBR-05, POL-004). 기존 구간을
            # 키우면 증량분이 원수요의 순번을 물려받아 다른 요청의 선순위를
            # 앞지른다.
            insert_increase_segment(
                session,
                line_id=state.line_id,
                sku_id=state.sku_id,
                seq=issue_demand_seq(session, state.sku_id),
                quantity=line_command.quantity - state.quantity,
            )
        else:
            freed = _reduce_line(session, state, state.quantity - line_command.quantity)
            if freed:
                released_skus.add(state.sku_id)
            released += freed

        update_line_quantity(session, state.line_id, line_command.quantity)
        changes.append(
            QuantityChange(
                line_id=state.line_id,
                previous_quantity=state.quantity,
                new_quantity=line_command.quantity,
            )
        )

    if not changes:
        return RevisionResult(RevisionOutcome.UNCHANGED, request_id=existing.id)

    insert_revisions(session, existing.id, uuid.uuid4(), changes, command)

    reallocated = 0
    for sku_id in sorted(released_skus):
        reallocated += allocate_sku(session, sku_id).allocated_quantity

    return RevisionResult(
        RevisionOutcome.APPLIED,
        request_id=existing.id,
        released_quantity=released,
        reallocated_quantity=reallocated,
    )

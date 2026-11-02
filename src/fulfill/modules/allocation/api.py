"""FIFO 배정 HTTP 입구 (BC-04).

JSON은 camelCase를 쓴다. 24-1·24-2와 같은 규약이다.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Response, status
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from fulfill.modules.allocation.domain import AllocationRunCommand
from fulfill.shared.db import unit_of_work
from fulfill.shared.http import to_camel_path
from fulfill.workflows.allocate import (
    AllocationOutcome,
    AllocationResult,
    LineAllocation,
    RequestAllocationSummary,
    run_allocation,
    summarize_request_allocation,
)
from fulfill.workflows.picking_readiness import PickingBlocker, evaluate_picking_readiness


class _Payload(BaseModel):
    """DTO 공통 설정. JSON은 camelCase, 파이썬 속성은 snake_case."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# UNCHANGED가 200인 것이 24-1의 재전달과 같은 이유다. 아무것도 생성되지 않았으므로
# 201이면 거짓이 된다. 재고가 없어 배정이 0건인 것은 정상 판정이며 거절이 아니다
# (NBR-01).
_STATUS_BY_OUTCOME = {
    AllocationOutcome.APPLIED: status.HTTP_201_CREATED,
    AllocationOutcome.UNCHANGED: status.HTTP_200_OK,
    AllocationOutcome.REJECTED: status.HTTP_422_UNPROCESSABLE_CONTENT,
}

RequestId = Annotated[uuid.UUID, Path(alias="requestId")]


class ProblemPayload(_Payload):
    field: str
    reason: str


class AllocationRunPayload(_Payload):
    """어느 판매사·품목의 큐를 다시 평가할지.

    수량도 요청 id도 받지 않는다. 배정 대상은 FIFO 순서가 정하므로 호출자가 "이
    요청에 N개 달라"고 지정할 수 없다. 지정할 수 있게 두면 그것이 AC-34가 금지한
    "우선 지정 실물의 직접 재배정"과 NBR-08의 긴급 예외로 가는 경로가 된다.
    """

    seller_code: str
    sku_code: str


class AllocationRunResponse(_Payload):
    """배정 실행 판정 응답.

    세 수치를 함께 담는다. AC-04가 가용·배정·부족을 구별하라고 요구하므로 배정량
    하나만 돌려주면 부분 배정과 전량 충족을 구분할 수 없다.
    """

    outcome: AllocationOutcome
    allocated_quantity: int
    unallocated_quantity: int
    allocatable_quantity: int
    problems: list[ProblemPayload] = []


class SegmentAllocationResponse(_Payload):
    """수요 구간 하나의 순번·요청 수량·배정 수량.

    순번을 노출하는 이유는 AC-03이 "요청 및 변경 접수 순서"를 업무 근거로
    요구하기 때문이다. 증량분이 뒤 순번을 받았다는 사실이 여기서 확인된다.
    """

    seq: int
    required_quantity: int
    allocated_quantity: int


class LineAllocationResponse(_Payload):
    sku_code: str
    required_quantity: int
    allocated_quantity: int
    unallocated_quantity: int
    segments: list[SegmentAllocationResponse]


class RequestAllocationResponse(_Payload):
    """요청 하나의 SKU별 배정 현황 (AC-04)."""

    lines: list[LineAllocationResponse]
    # 피킹 가능 여부가 아니다. 위치 확인과 HOLD 판정이 더 필요하며, 그 관문은
    # GET .../picking-readiness가 판정한다.
    all_lines_allocated: bool


def _to_run_response(result: AllocationResult) -> AllocationRunResponse:
    """조정 계층의 판정을 응답 DTO로 옮긴다."""
    return AllocationRunResponse(
        outcome=result.outcome,
        allocated_quantity=result.allocated_quantity,
        unallocated_quantity=result.unallocated_quantity,
        allocatable_quantity=result.allocatable_quantity,
        problems=[
            ProblemPayload(field=to_camel_path(problem.field), reason=problem.reason)
            for problem in result.problems
        ],
    )


def _to_line_response(line: LineAllocation) -> LineAllocationResponse:
    return LineAllocationResponse(
        sku_code=line.sku_code,
        required_quantity=line.required_quantity,
        allocated_quantity=line.allocated_quantity,
        unallocated_quantity=line.unallocated_quantity,
        segments=[
            SegmentAllocationResponse(
                seq=segment.seq,
                required_quantity=segment.required_quantity,
                allocated_quantity=segment.allocated_quantity,
            )
            for segment in line.segments
        ],
    )


def _to_summary_response(summary: RequestAllocationSummary) -> RequestAllocationResponse:
    return RequestAllocationResponse(
        lines=[_to_line_response(line) for line in summary.lines],
        all_lines_allocated=summary.all_lines_allocated,
    )


router = APIRouter(tags=["allocation"])


@router.post("/allocation-runs")
def run_fifo_allocation(payload: AllocationRunPayload, response: Response) -> AllocationRunResponse:
    """판매사+SKU의 미충족 수요에 적격 재고를 배정한다 (AC-03, AC-04).

    별도 명령인 것이 설계의 선택이다(결정 4). 검수 합격이 배정을 자동으로
    연쇄하지 않는다. AC-07은 "합격 570개만 FIFO 배정 대상이 된다"까지만 요구하고,
    연쇄시키면 한 입고의 검수 트랜잭션이 그 SKU의 FIFO 전체 평가와 guard 잠금을
    끌어안아 잠금 범위가 불필요하게 넓어진다.

    트랜잭션을 FastAPI 의존성으로 빼지 않는 이유는 24-1과 같다. 의존성의 종료
    코드는 응답 전송 뒤에 실행되므로 거기서 커밋이 실패하면 클라이언트는 롤백된
    작업에 대해 이미 201을 받은 상태가 된다.
    """
    with unit_of_work() as session:
        result = run_allocation(
            session,
            AllocationRunCommand(seller_code=payload.seller_code, sku_code=payload.sku_code),
        )

    response.status_code = _STATUS_BY_OUTCOME[result.outcome]
    return _to_run_response(result)


@router.get("/fulfillment-requests/{requestId}/allocations")
def read_request_allocation(request_id: RequestId) -> RequestAllocationResponse:
    """요청의 SKU별 필요·배정·미확보 수량을 보여 준다 (AC-04)."""
    with unit_of_work() as session:
        summary = summarize_request_allocation(session, request_id)

    if summary is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "등록되지 않은 출고 요청입니다")
    return _to_summary_response(summary)


class LineReadinessResponse(_Payload):
    """라인 하나의 관문 판정과 근거 수량.

    세 수량을 더해서 쓰면 안 된다. 위치 미확정이면서 보류인 재고는 두 수량에 모두
    들어가므로, 각각 "이 사유에 걸린 양"으로만 읽는다.
    """

    sku_code: str
    required_quantity: int
    allocated_quantity: int
    unallocated_quantity: int
    location_unconfirmed_quantity: int
    held_quantity: int
    blockers: list[PickingBlocker]
    ready: bool


class PickingReadinessResponse(_Payload):
    """요청의 피킹 준비 판정 (AC-05, AC-09).

    `ready`가 참이어도 작업이 시작되지는 않는다. 작업 수락·수집 실행은 AC-13~16의
    일이고 24단계 범위 밖이다.
    """

    request_id: uuid.UUID
    ready: bool
    blockers: list[PickingBlocker]
    lines: list[LineReadinessResponse]


@router.get("/fulfillment-requests/{requestId}/picking-readiness")
def read_picking_readiness(request_id: RequestId) -> PickingReadinessResponse:
    """요청이 정상 피킹에 착수할 수 있는지 판정한다 (AC-05, AC-09).

    `GET .../allocations` 옆에 두는 이유가 있다. 그 응답의 all_lines_allocated는
    전량 배정만 말하고 피킹 허가가 아니라고 주석에 적혀 있다. 실제 허가 판정을
    같은 자원 아래 두면 쓰는 쪽이 둘을 혼동하기 어렵다.
    """
    with unit_of_work() as session:
        readiness = evaluate_picking_readiness(session, request_id)

    if readiness is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "등록되지 않은 출고 요청입니다")
    return PickingReadinessResponse(
        request_id=readiness.request_id,
        ready=readiness.ready,
        blockers=list(readiness.blockers),
        lines=[
            LineReadinessResponse(
                sku_code=line.sku_code,
                required_quantity=line.required_quantity,
                allocated_quantity=line.allocated_quantity,
                unallocated_quantity=line.unallocated_quantity,
                location_unconfirmed_quantity=line.location_unconfirmed_quantity,
                held_quantity=line.held_quantity,
                blockers=list(line.blockers),
                ready=line.ready,
            )
            for line in readiness.lines
        ],
    )

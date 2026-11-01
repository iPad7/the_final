"""판매사+SKU FIFO 배정의 조정 계층 (TX-03).

이 모듈은 어느 모듈의 ORM도 직접 보지 않는다. 네 모듈이 공개한 입구만 쓴다 —
allocation의 guard·domain·repository·queries, requests의 수요 구간 조회,
inventory의 적격 버킷 잠금, seller_contracts의 판매사 조회.

TX-03이 "Seller+SKU의 적격 수량, HOLD, 기존 예약, 선순위 미충족 수요"를 함께
판정하라고 요구한 범위이며, 그 판정이 한 트랜잭션 안에서 일어난다.
"""

import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy.orm import Session

from fulfill.modules.allocation.domain import (
    AllocationRunCommand,
    DemandSlot,
    SupplySlot,
    plan_allocations,
    validate_allocation_run,
)
from fulfill.modules.allocation.guard import lock_sku_queue
from fulfill.modules.allocation.queries import sum_allocated_by_segment
from fulfill.modules.allocation.repository import insert_allocations
from fulfill.modules.inventory.queries import find_sku_codes_by_ids, find_sku_ids_by_codes
from fulfill.modules.inventory.repository import lock_allocatable_buckets, reserve_in_bucket
from fulfill.modules.requests.queries import list_demand_segments, summarize_request_demand
from fulfill.modules.seller_contracts.queries import find_seller_id_by_code
from fulfill.shared.validation import ValidationProblem, normalize_text


class AllocationOutcome(StrEnum):
    """배정 실행 명령의 판정 결과.

    재고가 없어 0개 배정된 것이 UNCHANGED인 것이 중요하다. 거절이 아니다.
    NBR-01과 AC-01이 "부족 재고만을 이유로 거절하지 않는다"고 요구하므로,
    이것을 REJECTED로 만들면 재고가 없는 정상 대기 상태가 오류로 보고된다.

    24-1의 CONFLICT는 없다. 배정에는 업무 멱등키가 없고, 같은 SKU를 두 번
    실행하는 것은 재전달이 아니라 "다시 평가해 달라"는 정상 요청이다. 두 번째
    실행은 배정할 것이 남지 않았으면 UNCHANGED로 끝난다.
    """

    # 1개 이상 새로 배정했다.
    APPLIED = "APPLIED"
    # 평가했고 배정할 것이 없었다. 적격 재고가 없거나 미충족 수요가 없다.
    UNCHANGED = "UNCHANGED"
    # 명령 자체가 유효하지 않다.
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class AllocationResult:
    """배정 판정과 그 근거.

    세 수치를 함께 담는다. AC-04가 "부분·미확보와 정상 배정을 구별"하고 "SKU별
    가용·배정·부족 수량"을 업무 근거로 요구하므로, 이번에 배정한 양만 돌려주면
    그 조건을 만족하지 못한다. 부분 배정으로 끝났는지 전량 충족됐는지가
    `unallocated_quantity`에서 드러난다.
    """

    outcome: AllocationOutcome
    # 이번 실행이 새로 배정한 수량.
    allocated_quantity: int = 0
    # 실행 후에도 남은 미충족 수요 합계. 0이 아니면 부분 배정이다.
    unallocated_quantity: int = 0
    # 실행 후 남은 배정 가능 재고. 수요보다 재고가 많으면 0이 아니다.
    allocatable_quantity: int = 0
    problems: tuple[ValidationProblem, ...] = field(default_factory=tuple)


def _rejected(field_name: str, reason: str) -> AllocationResult:
    """사유 하나로 거절한다."""
    return AllocationResult(
        AllocationOutcome.REJECTED, problems=(ValidationProblem(field_name, reason),)
    )


def _resolve_sku(session: Session, command: AllocationRunCommand) -> uuid.UUID | AllocationResult:
    """판매사·품목 코드를 SKU id로 해석한다.

    여기서는 코드가 없는 경우를 따로 다루지 않는다. 도메인이 둘 다 요구하므로
    빈 코드는 이 함수에 도달하지 않는다. 입고(AC-08)에서는 "조회할 것이 없는
    경우"가 정상 업무였지만, 배정은 어느 SKU의 큐를 평가할지 모르면 할 일이 없다.
    """
    seller_id = find_seller_id_by_code(session, normalize_text(command.seller_code))
    if seller_id is None:
        return _rejected("seller_code", "등록되지 않은 판매사입니다")

    sku_code = normalize_text(command.sku_code)
    sku_id = find_sku_ids_by_codes(session, seller_id, [sku_code]).get(sku_code)
    if sku_id is None:
        return _rejected("sku_code", "이 판매사에 등록되지 않은 SKU입니다")

    return sku_id


@dataclass(frozen=True)
class AllocationTotals:
    """배정 한 번의 결과 수치. 판정 어휘는 담지 않는다."""

    allocated_quantity: int
    unallocated_quantity: int
    allocatable_quantity: int


def allocate_sku(session: Session, sku_id: uuid.UUID) -> AllocationTotals:
    """이 SKU의 미충족 수요에 적격 재고를 배정한다 (AC-03, AC-04).

    코드 해석과 판정 어휘 없이 배정 자체만 한다. 수량 감소로 풀린 물량을 같은
    트랜잭션에서 선순위부터 재고려해야 하므로(CB-03) 수량 변경 조정 계층도 이
    함수를 쓴다.

    잠금 순서가 **guard → 버킷**이다. 이 순서를 모든 경로에서 똑같이 지켜야 한다.

    guard를 먼저 잡는 이유는 ADR23 §4가 정한 것이다. 버킷만 잠그면 두 트랜잭션이
    서로 다른 버킷을 각자 잡은 채 같은 수요 큐를 동시에 해석할 수 있고, 그러면
    둘 다 "선순위가 아직 미충족"이라고 읽어 각자 앞 구간을 채우려 든다. 결과는
    재고 초과이거나 FIFO 역전이다. 큐 하나에 판정 하나만 들어가게 만드는 것이
    guard의 일이고, 버킷 잠금은 그 안에서 재고 쪽 경합을 막는다.

    순서는 그 다음에 읽는다. 잠그기 전에 읽으면 읽은 값이 낡는다 — 24-2의 검수와
    같은 문제이고 같은 해법이다.

    HOLD는 아직 평가하지 않는다. TX-03이 함께 판정하라고 한 네 가지 중 적격 수량·
    기존 예약·선순위 미충족 수요는 여기 있지만 HOLD는 24-4의 몫이다. 그래서
    AC-04는 이 증분에서 부분 충족이다.
    """
    # ① 수요 큐를 직렬화한다. 이 SKU의 배정 판정은 한 번에 하나만 진행된다.
    lock_sku_queue(session, sku_id)
    # ② 적격 재고를 잠근다. 가용량을 읽은 뒤 예약하기까지 다른 배정이 끼어들지
    #    못한다.
    buckets = lock_allocatable_buckets(session, sku_id)

    segments = list_demand_segments(session, sku_id)
    allocated = sum_allocated_by_segment(session, sku_id)
    demands = tuple(
        DemandSlot(
            segment_id=segment.segment_id,
            seq=segment.seq,
            # 미충족 수량은 여기서 계산된다. demand_segment에 잔량 컬럼을 두지
            # 않은 결정(BC-02)의 대가이자 이점이다. 갱신을 누락할 컬럼이 없다.
            outstanding=segment.quantity - allocated.get(segment.segment_id, 0),
        )
        for segment in segments
    )
    supplies = tuple(
        SupplySlot(bucket_id=bucket.bucket_id, available=bucket.available) for bucket in buckets
    )

    plan = plan_allocations(demands, supplies)

    for item in plan:
        # 버킷의 배정량과 배정 사실, 둘을 같은 트랜잭션에서 쓴다. 한쪽만 커밋되면
        # 원장과 합계가 어긋난다.
        reserve_in_bucket(session, item.bucket_id, item.quantity)
    insert_allocations(session, sku_id, plan)

    just_allocated = sum(item.quantity for item in plan)
    return AllocationTotals(
        allocated_quantity=just_allocated,
        # 실행 전 수치에서 이번 배정분을 뺀다. 음수 미충족(초과 배정된 구간)은
        # 0으로 접어 합계가 왜곡되지 않게 한다.
        unallocated_quantity=sum(max(demand.outstanding, 0) for demand in demands) - just_allocated,
        allocatable_quantity=sum(supply.available for supply in supplies) - just_allocated,
    )


def run_allocation(session: Session, command: AllocationRunCommand) -> AllocationResult:
    """배정 실행 명령을 처리한다 (AC-03, AC-04).

    코드 해석과 판정 어휘만 담당하고, 배정 자체는 `allocate_sku`가 한다.
    """
    problems = validate_allocation_run(command)
    if problems:
        return AllocationResult(AllocationOutcome.REJECTED, problems=problems)

    resolved = _resolve_sku(session, command)
    if isinstance(resolved, AllocationResult):
        return resolved

    totals = allocate_sku(session, resolved)
    return AllocationResult(
        outcome=(
            AllocationOutcome.APPLIED if totals.allocated_quantity else AllocationOutcome.UNCHANGED
        ),
        allocated_quantity=totals.allocated_quantity,
        unallocated_quantity=totals.unallocated_quantity,
        allocatable_quantity=totals.allocatable_quantity,
    )


@dataclass(frozen=True)
class SegmentAllocation:
    """수요 구간 하나의 FIFO 순번·요청 수량·배정 수량."""

    seq: int
    required_quantity: int
    allocated_quantity: int


@dataclass(frozen=True)
class LineAllocation:
    """요청 라인 하나의 품목별 배정 현황."""

    sku_code: str
    required_quantity: int
    allocated_quantity: int
    segments: tuple[SegmentAllocation, ...]

    @property
    def unallocated_quantity(self) -> int:
        """미확보 수량. AC-04가 정상 배정과 구별하라고 요구하는 값이다."""
        return max(self.required_quantity - self.allocated_quantity, 0)


@dataclass(frozen=True)
class RequestAllocationSummary:
    """요청 하나의 SKU별 배정 현황 (AC-04)."""

    lines: tuple[LineAllocation, ...]

    @property
    def all_lines_allocated(self) -> bool:
        """모든 라인이 전량 배정됐는가.

        **피킹 가능 여부가 아니다.** AC-05·AC-09의 관문은 전량 배정에 더해 위치
        확인과 HOLD 없음을 요구하며 그 판정은 24-4의 일이다. 이 값을 피킹 허가로
        읽으면 "임시 구역 입고를 위치 확인 완료로 간주"하는 AC-09 위반이 된다.
        """
        return all(line.unallocated_quantity == 0 for line in self.lines)


def summarize_request_allocation(
    session: Session, request_id: uuid.UUID
) -> RequestAllocationSummary | None:
    """요청의 SKU별 필요·배정·미확보 수량을 돌려준다. None이면 그런 요청이 없다.

    세 모듈의 조회를 합친다. 라인과 수요 구간은 BC-02, 품목 코드는 BC-03, 배정
    합계는 BC-04가 가진 사실이고 어느 하나도 나머지를 볼 수 없다.

    잠그지 않는다. 현황 조회이며, 이 값을 근거로 쓰기를 결정하는 경로는 없다.
    """
    lines = summarize_request_demand(session, request_id)
    if lines is None:
        return None

    codes = find_sku_codes_by_ids(session, [line.sku_id for line in lines])
    summaries: list[LineAllocation] = []
    for line in lines:
        allocated = sum_allocated_by_segment(session, line.sku_id)
        segments = tuple(
            SegmentAllocation(
                seq=segment.seq,
                required_quantity=segment.quantity,
                allocated_quantity=allocated.get(segment.segment_id, 0),
            )
            for segment in line.segments
        )
        summaries.append(
            LineAllocation(
                sku_code=codes[line.sku_id],
                required_quantity=line.quantity,
                # 라인 수량이 아니라 구간별 배정의 합이다. 배정은 구간 단위로
                # 일어나므로 이 합만이 실제 배정량이다.
                allocated_quantity=sum(segment.allocated_quantity for segment in segments),
                segments=segments,
            )
        )

    return RequestAllocationSummary(lines=tuple(summaries))

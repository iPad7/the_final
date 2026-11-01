"""FIFO 배분 규칙 (AC-03, AC-04).

DB 없이 돌아간다. 배분이 순수 함수인 덕분이고, import 계약이 그것을 강제한다.

여기서 증명하는 것은 "순서를 지키는 계산"이다. 동시에 끼어드는 것을 막는 일은
잠금의 몫이고 실PG 경합 테스트에서 따로 다룬다. 둘은 다른 사실이다.
"""

import uuid

from fulfill.modules.allocation.domain import (
    AllocationRunCommand,
    DemandSlot,
    PlannedAllocation,
    SupplySlot,
    plan_allocations,
    validate_allocation_run,
)


def _id(suffix: int) -> uuid.UUID:
    """읽기 쉬운 고정 id. 실패 메시지에서 어느 구간·버킷인지 바로 보인다."""
    return uuid.UUID(f"00000000-0000-0000-0000-{suffix:012d}")


# AC-03의 Given을 그대로 옮긴 수요 집합이다. 판매사 A의 같은 SKU에
# FR-1 최초 2개(순번 1) → FR-2 1개(순번 2) → FR-1 추가 2개(순번 3).
#
# 증량이 순번 3인 것이 핵심이다. 같은 요청이라는 이유로 순번 1 옆에 붙이면
# NBR-05와 AC-03의 위반 조항("추가분을 최초 수요보다 앞에 놓지 않는다")을 깬다.
FR1_ORIGINAL = DemandSlot(segment_id=_id(1), seq=1, outstanding=2)
FR2_ORIGINAL = DemandSlot(segment_id=_id(2), seq=2, outstanding=1)
FR1_INCREASE = DemandSlot(segment_id=_id(3), seq=3, outstanding=2)

AC03_DEMANDS = (FR1_ORIGINAL, FR2_ORIGINAL, FR1_INCREASE)


def _allocated(plan: tuple[PlannedAllocation, ...]) -> dict[uuid.UUID, int]:
    """구간별 배정 합계. 어느 버킷에서 왔는지는 묶는다."""
    totals: dict[uuid.UUID, int] = {}
    for item in plan:
        totals[item.segment_id] = totals.get(item.segment_id, 0) + item.quantity
    return totals


def test_증량분은_다른_요청의_원수요보다_뒤에_배정된다() -> None:
    """AC-03: 적격 재고 3개는 FR-1 최초2 → FR-2 1 순서로 소진된다."""
    plan = plan_allocations(AC03_DEMANDS, (SupplySlot(bucket_id=_id(100), available=3),))

    assert _allocated(plan) == {_id(1): 2, _id(2): 1}
    # FR-1의 증량 구간은 같은 요청이지만 아무것도 받지 못한다. 받았다면 FR-2의
    # 선순위 수요를 건너뛴 것이다.
    assert _id(3) not in _allocated(plan)


def test_선순위가_못_채우면_후순위는_받지_못한다() -> None:
    """NBR-03의 금지 조항. 재고 1개는 전부 순번 1에게 간다."""
    plan = plan_allocations(AC03_DEMANDS, (SupplySlot(bucket_id=_id(100), available=1),))

    assert _allocated(plan) == {_id(1): 1}


def test_선순위는_못_채워도_부분_배정을_받는다() -> None:
    """NBR-07. 앞 테스트와 한 쌍이다.

    "건너뛰지 않는다"만 지키려면 아무에게도 주지 않는 구현으로도 통과한다. 선순위
    수요 2개에 재고 1개가 왔을 때 1개가 실제로 배정되는지 함께 못 박아야 그
    테스트가 의미를 갖는다.
    """
    plan = plan_allocations((FR1_ORIGINAL,), (SupplySlot(bucket_id=_id(100), available=1),))

    assert _allocated(plan) == {_id(1): 1}


def test_수요보다_재고가_많으면_수요만큼만_배정한다() -> None:
    """적격 재고 10개에 수요는 5개뿐이다. 남는 5개는 미배정으로 남는다."""
    plan = plan_allocations(AC03_DEMANDS, (SupplySlot(bucket_id=_id(100), available=10),))

    assert _allocated(plan) == {_id(1): 2, _id(2): 1, _id(3): 2}
    assert sum(item.quantity for item in plan) == 5


def test_한_구간이_여러_버킷에서_채워진다() -> None:
    """분할 입고가 정상 업무이므로 적격 수량은 여러 버킷에 흩어져 있다.

    수요 하나에 버킷 하나를 가정한 구현이었다면 여기서 깨진다.
    """
    plan = plan_allocations(
        (DemandSlot(segment_id=_id(1), seq=1, outstanding=5),),
        (
            SupplySlot(bucket_id=_id(100), available=3),
            SupplySlot(bucket_id=_id(200), available=2),
        ),
    )

    assert plan == (
        PlannedAllocation(segment_id=_id(1), bucket_id=_id(100), quantity=3),
        PlannedAllocation(segment_id=_id(1), bucket_id=_id(200), quantity=2),
    )


def test_한_버킷이_여러_구간으로_쪼개진다() -> None:
    """버킷 하나가 선순위를 채우고 남은 양으로 후순위를 이어서 채운다.

    버킷을 다 쓰지 않았는데 다음 버킷으로 넘어가면 재고를 남긴 채 수요를 미충족으로
    남기게 된다.
    """
    plan = plan_allocations(AC03_DEMANDS, (SupplySlot(bucket_id=_id(100), available=4),))

    assert plan == (
        PlannedAllocation(segment_id=_id(1), bucket_id=_id(100), quantity=2),
        PlannedAllocation(segment_id=_id(2), bucket_id=_id(100), quantity=1),
        PlannedAllocation(segment_id=_id(3), bucket_id=_id(100), quantity=1),
    )


def test_적격_재고가_없으면_빈_계획이_나온다() -> None:
    """거절이 아니다. 재고 없는 요청도 유효하게 대기한다(NBR-01, AC-01).

    조정 계층은 이 빈 계획을 UNCHANGED로 답한다.
    """
    assert plan_allocations(AC03_DEMANDS, ()) == ()


def test_미충족_수요가_없으면_빈_계획이_나온다() -> None:
    assert plan_allocations((), (SupplySlot(bucket_id=_id(100), available=10),)) == ()


def test_가용량이_0인_버킷은_배정에_쓰이지_않는다() -> None:
    """전량이 이미 다른 요청에 배정된 버킷이다. 0개를 배정하는 행을 만들지 않는다."""
    plan = plan_allocations(
        AC03_DEMANDS,
        (
            SupplySlot(bucket_id=_id(100), available=0),
            SupplySlot(bucket_id=_id(200), available=2),
        ),
    )

    assert plan == (PlannedAllocation(segment_id=_id(1), bucket_id=_id(200), quantity=2),)


def test_이미_전량_배정된_구간은_지나가고_뒤_구간이_받는다() -> None:
    """이것은 FIFO 건너뛰기가 아니다.

    선순위 구간에 미충족 수요가 **없는** 상태다. 건너뛰기 금지를 "앞 구간이 있으면
    뒤는 못 받는다"로 구현했다면 재배정이 영구히 막힌다. 금지되는 것은 미충족
    선순위를 두고 후순위를 채우는 일이다.
    """
    satisfied = DemandSlot(segment_id=_id(1), seq=1, outstanding=0)
    plan = plan_allocations(
        (satisfied, FR2_ORIGINAL), (SupplySlot(bucket_id=_id(100), available=5),)
    )

    assert _allocated(plan) == {_id(2): 1}


def test_입력_순서가_뒤섞여도_순번_순서로_배정한다() -> None:
    """정렬은 도메인이 한다.

    호출자가 정렬해 주기를 기대하면 FIFO 규칙의 소유자가 조회문의 ORDER BY가 된다.
    그러면 조회를 수정하는 사람이 규칙을 깨고, 그 사실이 이 모듈에는 보이지 않는다.
    """
    shuffled = (FR1_INCREASE, FR2_ORIGINAL, FR1_ORIGINAL)
    plan = plan_allocations(shuffled, (SupplySlot(bucket_id=_id(100), available=3),))

    assert _allocated(plan) == {_id(1): 2, _id(2): 1}


def test_판매사와_품목을_지정한_배정_명령은_유효하다() -> None:
    assert validate_allocation_run(AllocationRunCommand("SELLER-A", "SKU-P")) == ()


def test_판매사나_품목이_비면_배정_명령을_거절한다() -> None:
    """SKU는 판매사에 종속되므로 둘 다 필요하다. 어느 쪽이 비었는지 따로 알린다."""
    problems = validate_allocation_run(AllocationRunCommand("  ", ""))

    assert {problem.field for problem in problems} == {"seller_code", "sku_code"}

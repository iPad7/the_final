"""피킹 관문의 순수 판정 (AC-05, AC-09).

DB 없이 돌아간다. 관문의 규칙을 여기서 전수로 고정해 두면 통합 테스트는 "이 규칙이
올바른 입력을 받는가"만 보면 된다.
"""

from fulfill.workflows.picking_readiness import PickingBlocker, judge_line_blockers


def test_전량_배정되고_위치_확인되고_보류가_없으면_막히지_않는다() -> None:
    # AC-05의 정상 경로. 세 조건이 모두 성립한 유일한 상태다.
    assert (
        judge_line_blockers(
            required_quantity=3,
            allocated_quantity=3,
            location_unconfirmed_quantity=0,
            held_quantity=0,
        )
        == ()
    )


def test_부분_배정은_막힌다() -> None:
    # NBR-07: 부분 배정은 유지되지만 피킹·부분 출고는 불가다.
    assert judge_line_blockers(
        required_quantity=3,
        allocated_quantity=2,
        location_unconfirmed_quantity=0,
        held_quantity=0,
    ) == (PickingBlocker.UNALLOCATED,)


def test_배정이_전혀_없어도_막힘_사유는_하나다() -> None:
    # 배정이 0이면 위치도 보류도 걸릴 재고가 없다. 사유를 부풀리지 않는다.
    assert judge_line_blockers(
        required_quantity=3,
        allocated_quantity=0,
        location_unconfirmed_quantity=0,
        held_quantity=0,
    ) == (PickingBlocker.UNALLOCATED,)


def test_위치_미확정은_막힌다() -> None:
    # AC-09: 전량 배정됐어도 실제 피킹 위치가 확인되지 않으면 작업할 수 없다.
    assert judge_line_blockers(
        required_quantity=3,
        allocated_quantity=3,
        location_unconfirmed_quantity=3,
        held_quantity=0,
    ) == (PickingBlocker.LOCATION_UNCONFIRMED,)


def test_일부만_위치_미확정이어도_막힌다() -> None:
    # 전 SKU 전량이 피킹 가능해야 한다. 3개 중 1개를 못 꺼내면 정상 출고가 안 된다.
    assert judge_line_blockers(
        required_quantity=3,
        allocated_quantity=3,
        location_unconfirmed_quantity=1,
        held_quantity=0,
    ) == (PickingBlocker.LOCATION_UNCONFIRMED,)


def test_사용_보류는_막힌다() -> None:
    # NBR-14: 보류된 재고는 유효 배정을 유지하지만 작업에 쓰지 않는다.
    assert judge_line_blockers(
        required_quantity=3,
        allocated_quantity=3,
        location_unconfirmed_quantity=0,
        held_quantity=3,
    ) == (PickingBlocker.HELD,)


def test_세_사유가_동시에_뜰_수_있다() -> None:
    # 사유는 배타적이지 않다. 하나만 보고하면 담당자가 조치를 다 하지 못한다.
    assert judge_line_blockers(
        required_quantity=5,
        allocated_quantity=3,
        location_unconfirmed_quantity=2,
        held_quantity=1,
    ) == (
        PickingBlocker.UNALLOCATED,
        PickingBlocker.LOCATION_UNCONFIRMED,
        PickingBlocker.HELD,
    )


def test_위치_미확정과_보류가_같은_재고에_겹칠_수_있다() -> None:
    # 두 축이 독립이므로 같은 3개가 양쪽 수량에 들어간다. 더해서 6으로 읽으면
    # 중복 계산이다(CB-04가 경고한 성격).
    assert judge_line_blockers(
        required_quantity=3,
        allocated_quantity=3,
        location_unconfirmed_quantity=3,
        held_quantity=3,
    ) == (PickingBlocker.LOCATION_UNCONFIRMED, PickingBlocker.HELD)


def test_초과_배정은_막힘이_아니다() -> None:
    # 수요 감소 직후처럼 일시적으로 생길 수 있고 작업을 막을 근거가 아니다.
    assert (
        judge_line_blockers(
            required_quantity=3,
            allocated_quantity=4,
            location_unconfirmed_quantity=0,
            held_quantity=0,
        )
        == ()
    )

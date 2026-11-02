"""피킹 준비 관문을 실제 PostgreSQL에서 검증한다 (AC-05, AC-09).

세 조건을 하나씩 떼어내 각각 막히는지 본다. "모두 갖추면 통과"만 확인하면 아무
조건도 보지 않는 코드도 통과하므로, 금지 쪽을 조건별로 고정하는 것이 이 파일의
목적이다.

세 모듈의 조회를 합치는 경로이므로 순수 함수로는 보일 수 없다. 특히 "배정이 어느
버킷에 걸려 있는가"와 "그 버킷이 보류 입고묶음에 속하는가"는 DB만 답할 수 있다.
"""

import uuid

import pytest

from fulfill.modules.allocation.queries import list_allocated_positions
from fulfill.modules.requests.queries import summarize_request_demand
from fulfill.shared.db import unit_of_work
from fulfill.workflows.picking_readiness import (
    PickingBlocker,
    PickingReadiness,
    evaluate_picking_readiness,
)
from tests.scenarios import (
    confirm_location,
    lift_hold,
    make_passed_stock,
    place_hold,
    place_multi_line_request,
    place_request,
    revise_quantities,
    run_fifo,
)
from tests.seed_data import SKU_P, SKU_Q

pytestmark = pytest.mark.integration


def _readiness(request_id: uuid.UUID) -> PickingReadiness:
    """관문 판정을 읽는다. 요청이 없으면 테스트가 바로 깨진다."""
    with unit_of_work() as session:
        readiness = evaluate_picking_readiness(session, request_id)
    assert readiness is not None
    return readiness


def test_세_조건을_모두_갖추면_피킹할_수_있다(seeded_database: str) -> None:
    """AC-05의 정상 경로: 전량 확보 + 위치 확인 + 사용 제한 없음."""
    request_id = place_request("EXT-1", 3)
    receipt_id = make_passed_stock(3)
    run_fifo()
    confirm_location(receipt_id, "A-01")

    readiness = _readiness(request_id)

    assert readiness.ready
    assert readiness.blockers == ()
    assert readiness.lines[0].allocated_quantity == 3
    assert readiness.lines[0].unallocated_quantity == 0


def test_전량_확보되지_않으면_막힌다(seeded_database: str) -> None:
    """AC-05: 부분 배정은 유지하되 작업 착수는 허용하지 않는다."""
    request_id = place_request("EXT-1", 3)
    receipt_id = make_passed_stock(2)
    run_fifo()
    confirm_location(receipt_id, "A-01")

    readiness = _readiness(request_id)

    assert not readiness.ready
    assert readiness.blockers == (PickingBlocker.UNALLOCATED,)
    # 부분 배정 자체는 유지된다. 막는 것은 작업이지 배정이 아니다.
    assert readiness.lines[0].allocated_quantity == 2
    assert readiness.lines[0].unallocated_quantity == 1


def test_위치가_확인되지_않으면_막힌다(seeded_database: str) -> None:
    """AC-09: 임시 구역 입고를 위치 확인 완료로 간주하지 않는다.

    24-3이 증명한 "위치 미확정이어도 배정된다"의 반대쪽이다. 배정은 됐고 전량
    확보됐는데도 작업은 막혀야 한다.
    """
    request_id = place_request("EXT-1", 3)
    make_passed_stock(3)
    run_fifo()

    readiness = _readiness(request_id)

    assert not readiness.ready
    assert readiness.blockers == (PickingBlocker.LOCATION_UNCONFIRMED,)
    assert readiness.lines[0].unallocated_quantity == 0
    assert readiness.lines[0].location_unconfirmed_quantity == 3


def test_사용_보류_중이면_막힌다(seeded_database: str) -> None:
    """AC-04·NBR-14: 보류된 재고는 유효 배정을 유지하지만 작업에 쓰지 않는다."""
    request_id = place_request("EXT-1", 3)
    receipt_id = make_passed_stock(3)
    run_fifo()
    confirm_location(receipt_id, "A-01")
    place_hold(receipt_id)

    readiness = _readiness(request_id)

    assert not readiness.ready
    assert readiness.blockers == (PickingBlocker.HELD,)
    # 배정은 그대로다. 보류가 배정을 취소하지 않는다는 1단계의 결과가 여기서
    # "전량 확보됐으나 작업 불가"로 나타난다.
    assert readiness.lines[0].allocated_quantity == 3
    assert readiness.lines[0].held_quantity == 3


def test_보류를_풀면_다시_피킹할_수_있다(seeded_database: str) -> None:
    """AC-10: 관리자 확인 후 작업 가능 여부를 다시 검토한다."""
    request_id = place_request("EXT-1", 3)
    receipt_id = make_passed_stock(3)
    run_fifo()
    confirm_location(receipt_id, "A-01")
    place_hold(receipt_id)
    assert not _readiness(request_id).ready

    lift_hold(receipt_id)

    assert _readiness(request_id).ready


def test_위치를_확인하면_다시_피킹할_수_있다(seeded_database: str) -> None:
    """AC-09: "위치 확인 이후 작업 가능 여부를 다시 검토한다"."""
    request_id = place_request("EXT-1", 3)
    receipt_id = make_passed_stock(3)
    run_fifo()
    assert not _readiness(request_id).ready

    confirm_location(receipt_id, "A-01")

    assert _readiness(request_id).ready


def test_한_SKU만_막혀도_요청_전체가_막힌다(seeded_database: str) -> None:
    """AC-05의 금지 조항: 확보된 SKU만 먼저 담아 보내지 않는다.

    AC-05의 Given 그대로다 — 서로 다른 SKU 두 개가 필요한데 한 SKU가 1개 부족하다.
    """
    request_id = place_multi_line_request("EXT-1", {SKU_P: 2, SKU_Q: 2})
    ready_receipt = make_passed_stock(2, sku_code=SKU_P)
    short_receipt = make_passed_stock(1, sku_code=SKU_Q)
    run_fifo(sku_code=SKU_P)
    run_fifo(sku_code=SKU_Q)
    confirm_location(ready_receipt, "A-01")
    confirm_location(short_receipt, "B-01")

    readiness = _readiness(request_id)

    assert not readiness.ready
    assert readiness.blockers == (PickingBlocker.UNALLOCATED,)
    by_code = {line.sku_code: line for line in readiness.lines}
    # 한쪽 라인은 그 자체로는 작업 가능하다. 그래도 요청은 막힌다.
    assert by_code[SKU_P].ready
    assert not by_code[SKU_Q].ready
    assert by_code[SKU_Q].unallocated_quantity == 1


def test_위치_미확정과_보류가_함께_보고된다(seeded_database: str) -> None:
    """두 축은 독립이므로 같은 재고가 양쪽에 걸릴 수 있다.

    사유를 하나만 보고하면 담당자가 조치를 절반만 하고 다시 막힌다.
    """
    request_id = place_request("EXT-1", 3)
    receipt_id = make_passed_stock(3)
    run_fifo()
    place_hold(receipt_id)

    readiness = _readiness(request_id)

    assert readiness.blockers == (PickingBlocker.LOCATION_UNCONFIRMED, PickingBlocker.HELD)
    line = readiness.lines[0]
    # 같은 3개가 두 수량에 모두 들어간다. 더해서 6으로 읽으면 중복 계산이다.
    assert line.location_unconfirmed_quantity == 3
    assert line.held_quantity == 3


def test_전량_해제된_배정의_버킷은_관문을_막지_않는다(seeded_database: str) -> None:
    """해제된 배정이 남긴 버킷은 이미 이 요청의 재고가 아니다.

    24-3의 수량 감소는 배정 행을 지우지 않고 released_quantity를 올린다. 그래서 그
    버킷이 위치 미확정이어도 관문에 들어오면 안 된다.

    그 상태를 만들려면 해제될 배정이 위치 미확정 버킷에 걸려 있어야 한다. 증량분이
    두 번째 입고에서 배정받게 한 뒤, 감량으로 그 구간을 0으로 만든다 — 감량은 최신
    구간부터이므로(NBR-05) 풀리는 것은 증량분이다.
    """
    request_id = place_request("EXT-1", 1)
    located = make_passed_stock(1)
    run_fifo()
    confirm_location(located, "A-01")
    assert _readiness(request_id).ready

    # 증량분은 위치를 확인하지 않은 두 번째 입고에서 배정받는다.
    revise_quantities("EXT-1", {SKU_P: 2})
    make_passed_stock(1)
    run_fifo()
    assert _readiness(request_id).blockers == (PickingBlocker.LOCATION_UNCONFIRMED,)

    # 다시 1개로 줄이면 증량 구간이 0이 되고 그 배정이 전량 해제된다.
    revise_quantities("EXT-1", {SKU_P: 1})

    readiness = _readiness(request_id)
    assert readiness.ready
    assert readiness.lines[0].location_unconfirmed_quantity == 0


def test_전량_해제된_버킷은_배정_버킷_목록에서_빠진다(seeded_database: str) -> None:
    """`list_allocated_positions`의 계약을 직접 고정한다.

    관문은 수량을 합산하므로 수량 0인 행이 섞여도 판정이 바뀌지 않는다. 그래서 위
    테스트만으로는 이 조건이 지켜지는지 알 수 없다. 질의가 "유효 배정이 걸린
    버킷"을 돌려준다고 말했으니 그 말 자체를 검증한다.
    """
    request_id = place_request("EXT-1", 1)
    make_passed_stock(1)
    run_fifo()
    revise_quantities("EXT-1", {SKU_P: 2})
    make_passed_stock(1)
    run_fifo()

    with unit_of_work() as session:
        lines = summarize_request_demand(session, request_id)
        assert lines is not None
        segment_ids = [segment.segment_id for segment in lines[0].segments]
        assert len(list_allocated_positions(session, segment_ids)) == 2

    revise_quantities("EXT-1", {SKU_P: 1})

    with unit_of_work() as session:
        # 구간은 둘 다 남아 있지만 배정이 걸린 버킷은 하나다.
        assert len(segment_ids) == 2
        assert len(list_allocated_positions(session, segment_ids)) == 1


def test_없는_요청은_판정하지_않는다(seeded_database: str) -> None:
    """없는 요청에 "준비 안 됨"을 주면 조회 실패와 구별되지 않는다."""
    missing = uuid.UUID("00000000-0000-0000-0000-0000000000ff")

    with unit_of_work() as session:
        assert evaluate_picking_readiness(session, missing) is None

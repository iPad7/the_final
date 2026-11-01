"""재고 사용 보류를 실제 PostgreSQL에서 검증한다 (AC-04, AC-10).

이 파일이 증명할 것은 두 가지다. 보류가 신규 배정 자격을 없애는가, 그리고 **이미
붙어 있는 배정을 건드리지 않는가.** 후자가 AC-04의 금지 조항이며 순수 함수로는
보일 수 없다 — 보류는 코드가 아무 일도 하지 않음으로써 배정을 보존하므로, 그
"아무 일도 하지 않음"을 DB 상태로 확인해야 한다.

활성 보류의 유일성은 부분 유니크 인덱스가 판정하므로 SQLite로는 검증할 수 없다
(ADR23-03/07).
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from fulfill.modules.inventory.domain import HOLD_REASON_OTHER, HOLD_REASON_QUALITY_SUSPECTED
from fulfill.modules.inventory.queries import find_active_hold, find_allocatable_quantity
from fulfill.shared.db import unit_of_work
from fulfill.workflows.allocate import AllocationOutcome, summarize_request_allocation
from fulfill.workflows.hold import HoldOutcome
from tests.scenarios import (
    allocated_for,
    lift_hold,
    make_passed_stock,
    place_hold,
    place_request,
    run_fifo,
    sku_id_of,
)

pytestmark = pytest.mark.integration


def test_입고묶음에_보류를_걸_수_있다(seeded_database: str) -> None:
    """AC-10: 조사 착수를 기록한다. 손실 확정과는 다른 사건이다."""
    receipt_id = make_passed_stock(50)

    result = place_hold(receipt_id)

    assert result.outcome is HoldOutcome.APPLIED
    assert result.hold_id is not None

    with unit_of_work() as session:
        hold = find_active_hold(session, receipt_id)
    assert hold is not None
    assert hold.held_by == "WH-MANAGER-01"
    # AC-10이 요구하는 조사 근거가 조회로 돌아온다.
    assert hold.note == "기록과 실물 수량이 다름"


def test_이미_보류된_입고묶음은_변화가_없다(seeded_database: str) -> None:
    """중복 보류는 거절이 아니다. 이미 달성된 상태다."""
    receipt_id = make_passed_stock(50)
    first = place_hold(receipt_id)

    second = place_hold(receipt_id, reason=HOLD_REASON_QUALITY_SUSPECTED)

    assert second.outcome is HoldOutcome.UNCHANGED
    assert second.hold_id is None
    # 두 번째 사유로 덮어쓰지 않는다. 먼저 걸린 보류가 그대로 유효하다.
    with unit_of_work() as session:
        hold = find_active_hold(session, receipt_id)
    assert hold is not None
    assert hold.hold_id == first.hold_id


def test_보류를_풀_수_있다(seeded_database: str) -> None:
    """AC-10: 관리자가 확인을 마치면 해제한다."""
    receipt_id = make_passed_stock(50)
    placed = place_hold(receipt_id)

    lifted = lift_hold(receipt_id)

    assert lifted.outcome is HoldOutcome.APPLIED
    assert lifted.hold_id == placed.hold_id
    with unit_of_work() as session:
        assert find_active_hold(session, receipt_id) is None


def test_활성_보류가_없으면_해제는_변화가_없다(seeded_database: str) -> None:
    """풀려 있는 재고를 다시 푸는 것은 오류가 아니다."""
    receipt_id = make_passed_stock(50)

    assert lift_hold(receipt_id).outcome is HoldOutcome.UNCHANGED


def test_없는_입고묶음의_보류는_거절한다(seeded_database: str) -> None:
    """대상이 없는 것과 보류가 없는 것은 다른 결론이다."""
    missing = uuid.UUID("00000000-0000-0000-0000-0000000000ff")

    placed = place_hold(missing, reason=HOLD_REASON_OTHER, note="대상 확인")
    lifted = lift_hold(missing)

    assert placed.outcome is HoldOutcome.REJECTED
    assert [problem.field for problem in placed.problems] == ["receipt_id"]
    assert lifted.outcome is HoldOutcome.REJECTED


def test_보류된_재고는_신규_배정에서_제외된다(seeded_database: str) -> None:
    """NBR-14: 영향 가능 범위를 사용 보류한다."""
    place_request("EXT-1", 3)
    receipt_id = make_passed_stock(3)
    place_hold(receipt_id)

    result = run_fifo()

    # 거절이 아니다. 요청은 유효하고 지금 줄 수 있는 재고가 없을 뿐이다(AC-01).
    assert result.outcome is AllocationOutcome.UNCHANGED
    assert result.allocated_quantity == 0
    with unit_of_work() as session:
        assert find_allocatable_quantity(session, sku_id_of(session)) == 0


def test_보류는_이미_붙은_배정을_건드리지_않는다(seeded_database: str) -> None:
    """AC-04: 조사 의심 단계에서 유효 배정을 무단 삭제하지 않는다."""
    request_id = place_request("EXT-1", 3)
    receipt_id = make_passed_stock(3)
    run_fifo()
    assert allocated_for(request_id) == 3

    place_hold(receipt_id)

    # 보류 뒤에도 배정은 그대로다. 손실이 확정되면 배정을 조정하지만(NBR-13),
    # 조사 착수는 확정이 아니다.
    assert allocated_for(request_id) == 3
    with unit_of_work() as session:
        summary = summarize_request_allocation(session, request_id)
    assert summary is not None
    assert summary.all_lines_allocated


def test_보류는_다른_입고묶음에_영향을_주지_않는다(seeded_database: str) -> None:
    """AC-10: 무관 재고를 자동 차단하지 않는다."""
    place_request("EXT-1", 5)
    held = make_passed_stock(2)
    make_passed_stock(3)

    place_hold(held)
    result = run_fifo()

    # 보류되지 않은 입고묶음의 3개만 배정된다. 범위 단위가 입고묶음이므로 같은
    # SKU라도 다른 입고묶음은 걸리지 않는다.
    assert result.outcome is AllocationOutcome.APPLIED
    assert result.allocated_quantity == 3
    assert result.unallocated_quantity == 2


def test_해제하면_다시_배정_자격을_얻는다(seeded_database: str) -> None:
    """AC-10: 관리자 확인 후 정상 재고로 돌아온다."""
    request_id = place_request("EXT-1", 3)
    receipt_id = make_passed_stock(3)
    place_hold(receipt_id)
    assert run_fifo().allocated_quantity == 0

    lift_hold(receipt_id)

    # 해제 자체가 배정하지는 않는다. 다음 배정 실행이 FIFO 순서대로 가져간다.
    assert allocated_for(request_id) == 0
    assert run_fifo().allocated_quantity == 3
    assert allocated_for(request_id) == 3


def test_가용량_조회와_배정이_같은_보류_판정을_쓴다(seeded_database: str) -> None:
    """두 입구의 자격 조건이 갈라지면 화면과 결과가 어긋난다."""
    place_request("EXT-1", 4)
    held = make_passed_stock(1)
    make_passed_stock(3)
    place_hold(held)

    with unit_of_work() as session:
        visible = find_allocatable_quantity(session, sku_id_of(session))

    # 조회가 보여 준 수량이 실제로 배정된다. 한쪽만 보류를 거르면 이 둘이 달라진다.
    assert visible == 3
    assert run_fifo().allocated_quantity == visible


def test_활성_보류는_입고묶음당_하나다(seeded_database: str) -> None:
    """부분 유니크 인덱스가 중복 활성 보류를 거부한다.

    애플리케이션 경로는 ON CONFLICT로 UNCHANGED를 돌려주므로 이 제약이 실제로
    존재하는지 보여 주지 못한다. 직접 INSERT해 DB가 막는 것을 확인한다.
    """
    receipt_id = make_passed_stock(50)
    place_hold(receipt_id)

    with pytest.raises(IntegrityError), unit_of_work() as session:
        session.execute(
            text(
                "INSERT INTO inventory_hold"
                " (id, receipt_id, reason, held_by, environment_class, occurred_at)"
                " VALUES (gen_random_uuid(), :receipt_id, 'OTHER', 'X', 'SIMULATION', now())"
            ),
            {"receipt_id": receipt_id},
        )


def test_해제_시각만_채운_보류는_거부된다(seeded_database: str) -> None:
    """ck_hold_lift_fields_together: 주체 없는 해제를 막는다.

    활성 판정이 lifted_at 하나만 보므로, 이 제약이 없으면 주체와 기록 시각 없이
    풀린 보류가 조용히 활성에서 빠진다(AC-36의 판단 주체·시점 보존 위반).
    """
    receipt_id = make_passed_stock(50)

    with pytest.raises(IntegrityError), unit_of_work() as session:
        session.execute(
            text(
                "INSERT INTO inventory_hold"
                " (id, receipt_id, reason, held_by, environment_class, occurred_at, lifted_at)"
                " VALUES (gen_random_uuid(), :receipt_id, 'OTHER', 'X', 'SIMULATION',"
                " now(), now())"
            ),
            {"receipt_id": receipt_id},
        )


def test_해제_후_다시_보류할_수_있다(seeded_database: str) -> None:
    """재보류는 같은 사실의 연장이 아니라 새 사실이다.

    부분 인덱스의 WHERE 절이 하는 일이 여기서 드러난다. 조건 없는 유니크였다면
    입고묶음당 보류를 평생 한 번만 걸 수 있었다.
    """
    receipt_id = make_passed_stock(50)
    first = place_hold(receipt_id)
    lift_hold(receipt_id)

    second = place_hold(receipt_id, reason=HOLD_REASON_QUALITY_SUSPECTED, note=None)

    assert second.outcome is HoldOutcome.APPLIED
    assert second.hold_id != first.hold_id
    # 과거 보류 이력이 남아 있다. 해제가 사실을 지우지 않는다(AC-36).
    with unit_of_work() as session:
        total = session.execute(
            text("SELECT count(*) FROM inventory_hold WHERE receipt_id = :receipt_id"),
            {"receipt_id": receipt_id},
        ).scalar_one()
    assert total == 2


def test_이미_해제된_보류를_다시_해제하지_않는다(seeded_database: str) -> None:
    """AC-36: 나중 해제가 앞선 해제 주체·시각을 덮어쓰지 않는다.

    "한 번도 보류되지 않았다"와 다른 경로다. 해제 대상 행이 실제로 있고, 다만
    이미 풀려 있다. UPDATE 조건에서 활성 판정이 빠지면 이 행이 다시 갱신되어
    처음 누가 언제 풀었는지가 사라진다.
    """
    receipt_id = make_passed_stock(50)
    place_hold(receipt_id)
    lift_hold(receipt_id)

    with unit_of_work() as session:
        first_lift = session.execute(
            text("SELECT lifted_by, lifted_at FROM inventory_hold WHERE receipt_id = :receipt_id"),
            {"receipt_id": receipt_id},
        ).one()

    assert lift_hold(receipt_id).outcome is HoldOutcome.UNCHANGED

    with unit_of_work() as session:
        after = session.execute(
            text("SELECT lifted_by, lifted_at FROM inventory_hold WHERE receipt_id = :receipt_id"),
            {"receipt_id": receipt_id},
        ).one()
    assert after == first_lift

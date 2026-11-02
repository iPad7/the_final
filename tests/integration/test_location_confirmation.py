"""피킹 위치 확인을 실제 PostgreSQL에서 검증한다 (AC-09).

증명할 것이 세 가지다. 위치가 제자리 갱신되는가, 확인 전 위치가 사실로 남는가,
그리고 **기존 배정이 그대로인가.** 세 번째가 A안(제자리 갱신)을 고른 이유이며,
수량 이동 방식이었다면 배정이 붙은 버킷에서 CHECK 제약에 걸렸을 자리다.

부분 검수로 합격 버킷이 갈라지는 경로도 여기서 고정한다. uq_stock_bucket의
NULLS NOT DISTINCT 동작에 의존하므로 SQLite로는 검증할 수 없다(ADR23-03/07).
"""

import uuid

import pytest
from sqlalchemy import text

from fulfill.modules.inventory.domain import QC_PASS
from fulfill.shared.db import unit_of_work
from fulfill.workflows.confirm_location import LocationOutcome
from tests.scenarios import (
    allocated_for,
    confirm_location,
    judge_qc,
    make_passed_stock,
    place_request,
    receive_stock,
    run_fifo,
)

pytestmark = pytest.mark.integration


def _bucket_locations(receipt_id: uuid.UUID) -> list[tuple[str | None, int]]:
    """입고묶음의 합격 버킷을 (위치, 수량)으로 읽는다."""
    with unit_of_work() as session:
        rows = session.execute(
            text(
                "SELECT location_code, quantity FROM stock_position"
                " WHERE receipt_id = :receipt_id AND qc_state = 'PASSED'"
                " ORDER BY location_code NULLS FIRST"
            ),
            {"receipt_id": receipt_id},
        ).all()
    return [(row[0], row[1]) for row in rows]


def test_위치_미확정_합격_재고의_위치를_확정한다(seeded_database: str) -> None:
    """AC-09: 임시 구역에 있던 합격품의 실제 피킹 위치를 확인한다."""
    receipt_id = make_passed_stock(100)
    assert _bucket_locations(receipt_id) == [(None, 100)]

    result = confirm_location(receipt_id, "A-01")

    assert result.outcome is LocationOutcome.APPLIED
    assert result.confirmed_quantity == 100
    # 버킷이 갈라지지 않고 같은 행의 위치만 바뀐다.
    assert _bucket_locations(receipt_id) == [("A-01", 100)]


def test_확인_전_위치가_사실로_남는다(seeded_database: str) -> None:
    """AC-09: "임시 보관 위치"와 "확인 사실" 둘을 모두 보존한다.

    버킷의 location_code는 제자리 갱신되어 과거 값이 사라지므로, 이 사실 행만이
    "어디에서 어디로 확인됐는가"의 증거다.
    """
    receipt_id = make_passed_stock(100)

    confirm_location(receipt_id, "A-01")

    with unit_of_work() as session:
        row = session.execute(
            text(
                "SELECT previous_location_code, location_code, confirmed_by"
                " FROM location_confirmation WHERE receipt_id = :receipt_id"
            ),
            {"receipt_id": receipt_id},
        ).one()
    assert row[0] is None
    assert row[1] == "A-01"
    assert row[2] == "WH-STAFF-02"


def test_같은_위치_재확인은_변화가_없다(seeded_database: str) -> None:
    """현장 확인 보고가 두 번 올라오는 일은 사고가 아니다."""
    receipt_id = make_passed_stock(100)
    confirm_location(receipt_id, "A-01")

    again = confirm_location(receipt_id, "A-01")

    assert again.outcome is LocationOutcome.UNCHANGED
    assert _bucket_locations(receipt_id) == [("A-01", 100)]
    # 사실도 한 번만 쌓인다. 변화가 없는데 사건을 기록하면 이력이 거짓이 된다.
    with unit_of_work() as session:
        total = session.execute(
            text("SELECT count(*) FROM location_confirmation WHERE receipt_id = :receipt_id"),
            {"receipt_id": receipt_id},
        ).scalar_one()
    assert total == 1


def test_다른_위치로_옮기는_요청은_거절한다(seeded_database: str) -> None:
    """재배치는 실물 이동이 선행되는 별도 업무이며 AC-09의 범위가 아니다."""
    receipt_id = make_passed_stock(100)
    confirm_location(receipt_id, "A-01")

    moved = confirm_location(receipt_id, "B-02")

    assert moved.outcome is LocationOutcome.REJECTED
    assert [problem.field for problem in moved.problems] == ["location_code"]
    assert _bucket_locations(receipt_id) == [("A-01", 100)]


def test_부분_검수로_갈라진_합격_재고는_다른_위치로_확정한다(seeded_database: str) -> None:
    """합격 판정이 실물을 옮기지 않으므로 늦게 합격한 물량은 임시 구역에 남는다.

    570개를 확인해 A-01로 확정한 뒤 나머지 20개가 합격하면, 그 20개는 위치 NULL인
    합격 버킷으로 새로 생긴다. 실제로 선반에 올려 둔 자리를 확정할 수 있어야 한다.
    """
    receipt_id = receive_stock(590)
    judge_qc(receipt_id, QC_PASS, 570)
    confirm_location(receipt_id, "A-01")
    judge_qc(receipt_id, QC_PASS, 20)
    assert _bucket_locations(receipt_id) == [(None, 20), ("A-01", 570)]

    result = confirm_location(receipt_id, "A-02")

    assert result.outcome is LocationOutcome.APPLIED
    assert result.confirmed_quantity == 20
    assert _bucket_locations(receipt_id) == [("A-01", 570), ("A-02", 20)]


def test_갈라진_합격_재고를_같은_위치로_합치는_요청은_거절한다(seeded_database: str) -> None:
    """버킷 병합은 24-4의 범위가 아니다.

    수량과 배정 합계를 옮기고 allocation이 가리키는 버킷을 재지정해야 하는데, 어느
    배정이 어느 버킷을 따라가는지는 어떤 문서도 정하지 않았다. 임의로 정하지 않고
    사유를 밝혀 거절한다 — 다른 위치로는 확정되므로 막히는 것은 합치기뿐이다.
    """
    receipt_id = receive_stock(590)
    judge_qc(receipt_id, QC_PASS, 570)
    confirm_location(receipt_id, "A-01")
    judge_qc(receipt_id, QC_PASS, 20)

    merged = confirm_location(receipt_id, "A-01")

    assert merged.outcome is LocationOutcome.REJECTED
    assert [problem.field for problem in merged.problems] == ["location_code"]
    # 상태가 바뀌지 않는다. 거절이 절반만 적용되면 수량 보존이 깨진다.
    assert _bucket_locations(receipt_id) == [(None, 20), ("A-01", 570)]


def test_합격_재고가_없으면_위치를_확정할_수_없다(seeded_database: str) -> None:
    """AC-09는 합격품의 위치를 다룬다. 미검수 물량은 대상이 아니다."""
    receipt_id = receive_stock(100)

    result = confirm_location(receipt_id, "A-01")

    assert result.outcome is LocationOutcome.REJECTED
    assert [problem.field for problem in result.problems] == ["receipt_id"]


def test_없는_입고묶음의_위치_확인은_거절한다(seeded_database: str) -> None:
    missing = uuid.UUID("00000000-0000-0000-0000-0000000000ff")

    result = confirm_location(missing, "A-01")

    assert result.outcome is LocationOutcome.REJECTED
    assert [problem.field for problem in result.problems] == ["receipt_id"]


def test_위치_확인은_기존_배정을_건드리지_않는다(seeded_database: str) -> None:
    """A안(제자리 갱신)을 고른 이유다.

    NBR-12 때문에 배정이 위치 확인보다 먼저 일어나는 것이 정상 순서다. 수량 이동
    방식이었다면 원래 버킷의 quantity가 줄어 ck_stock_allocated_within_quantity가
    커밋을 거부했을 것이고, 배정을 재지정하는 코드가 필요해졌을 것이다.
    """
    request_id = place_request("EXT-1", 100)
    receipt_id = make_passed_stock(100)
    run_fifo()
    assert allocated_for(request_id) == 100

    assert confirm_location(receipt_id, "A-01").outcome is LocationOutcome.APPLIED

    assert allocated_for(request_id) == 100
    # 버킷의 배정 합계도 그대로다. 위치는 바뀌었지만 같은 물건이다.
    with unit_of_work() as session:
        allocated = session.execute(
            text(
                "SELECT allocated_quantity FROM stock_position"
                " WHERE receipt_id = :receipt_id AND qc_state = 'PASSED'"
            ),
            {"receipt_id": receipt_id},
        ).scalar_one()
    assert allocated == 100

"""수량 증감과 정당 해제를 실제 PostgreSQL에서 검증한다 (AC-03, TC-04, TC-05).

증량은 변경 시점의 새 FIFO 구간이 되고(NBR-05, POL-004), 감량은 최신 구간부터
줄어든다(D-01). 감량으로 풀린 물량은 같은 트랜잭션에서 그 시점의 선순위부터 다시
배정된다(CB-03).
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from fulfill.shared.db import unit_of_work
from fulfill.workflows.revise_demand import RevisionOutcome
from tests.scenarios import (
    allocated_for,
    allocation_summary_of,
    make_passed_stock,
    place_multi_line_request,
    place_request,
    revise_quantities,
    run_fifo,
)
from tests.seed_data import SKU_P, SKU_Q

pytestmark = pytest.mark.integration


def _segments(request_id: uuid.UUID) -> list[tuple[int, int, int]]:
    """요청의 (순번, 요청 수량, 배정 수량). 라인이 하나인 테스트에서 쓴다."""
    line = allocation_summary_of(request_id).lines[0]
    return [
        (segment.seq, segment.required_quantity, segment.allocated_quantity)
        for segment in line.segments
    ]


def test_증량분은_변경_시점의_후순위를_받는다(seeded_database: str) -> None:
    """TC-04 · AC-03의 대표 시나리오다.

    FR-1 최초 2개 → FR-2 1개 → FR-1 추가 2개. 적격 재고 3개는 FR-1 최초2 →
    FR-2 1 순서로 가고 추가분은 기다린다. 추가분이 FR-2보다 앞에 오면 AC-03의
    위반 조항("긴급 표시·유효 기한 변경으로 추가분을 최초 수요보다 앞에 놓지
    않는다")을 깬다.
    """
    first = place_request("EXT-1", 2)
    second = place_request("EXT-2", 1)

    revision = revise_quantities("EXT-1", {SKU_P: 4})
    assert revision.outcome is RevisionOutcome.APPLIED
    # 증량만 일어났으므로 풀린 것도 재배정된 것도 없다.
    assert (revision.released_quantity, revision.reallocated_quantity) == (0, 0)

    make_passed_stock(3)
    run_fifo()

    assert (allocated_for(first), allocated_for(second)) == (2, 1)
    # 순번 1(원수요 2)과 순번 3(증량 2). FR-2의 순번 2가 그 사이에 있다.
    assert _segments(first) == [(1, 2, 2), (3, 2, 0)]
    assert _segments(second) == [(2, 1, 1)]

    # 재고가 더 들어오면 그때 증량분이 채워진다.
    make_passed_stock(2)
    run_fifo()

    assert _segments(first) == [(1, 2, 2), (3, 2, 2)]
    assert allocated_for(first) == 4


def test_증량은_기존_구간을_키우지_않고_새_구간을_만든다(seeded_database: str) -> None:
    """NBR-05의 구조적 요구. 기존 구간을 키우면 증량분이 원수요의 순번을 물려받는다."""
    request_id = place_request("EXT-1", 2)

    revise_quantities("EXT-1", {SKU_P: 5})

    with unit_of_work() as session:
        kinds = session.execute(
            text("SELECT kind, quantity, seq FROM demand_segment ORDER BY seq")
        ).all()

    assert [(row[0], row[1]) for row in kinds] == [("ORIGINAL", 2), ("INCREASE", 3)]
    assert _segments(request_id) == [(1, 2, 0), (2, 3, 0)]


def test_감량은_최신_구간의_배정부터_푼다(seeded_database: str) -> None:
    """D-01, POL-004. 원수요 2 + 증량 2가 전량 배정된 상태에서 3으로 줄인다.

    풀려야 하는 것은 증량 구간의 1개다. 오래된 구간을 먼저 깎으면 선순위 수요가
    사라지고 그 자리를 후순위가 차지하게 되어 FIFO가 뒤집힌다.
    """
    request_id = place_request("EXT-1", 2)
    revise_quantities("EXT-1", {SKU_P: 4})
    make_passed_stock(4)
    run_fifo()
    assert _segments(request_id) == [(1, 2, 2), (2, 2, 2)]

    revision = revise_quantities("EXT-1", {SKU_P: 3})

    assert revision.released_quantity == 1
    # 원수요 구간은 그대로고 증량 구간만 줄었다.
    assert _segments(request_id) == [(1, 2, 2), (2, 1, 1)]


def test_풀린_물량은_그_시점의_선순위로_재배정된다(seeded_database: str) -> None:
    """TC-05 · CB-03. 다른 요청의 정상 배정은 건드리지 않는다.

    적격 재고 5개가 FR-1에 2개, FR-2에 3개 배정되고 FR-3은 아무것도 받지 못한
    상태다. FR-1이 1개로 줄면 1개가 풀리고, 그 1개는 **그 시점의** 선순위 미충족
    구간인 FR-3으로 간다.

    FR-2의 배정 3개가 그대로 남는 것이 NBR-04의 금지 조항이다. 재배정이 다른
    요청의 몫을 건드리면 "유효 배정 강제 회수"가 된다.
    """
    first = place_request("EXT-1", 2)
    second = place_request("EXT-2", 3)
    third = place_request("EXT-3", 2)
    make_passed_stock(5)
    run_fifo()
    assert (allocated_for(first), allocated_for(second), allocated_for(third)) == (2, 3, 0)

    revision = revise_quantities("EXT-1", {SKU_P: 1})

    assert revision.outcome is RevisionOutcome.APPLIED
    assert (revision.released_quantity, revision.reallocated_quantity) == (1, 1)
    assert (allocated_for(first), allocated_for(second), allocated_for(third)) == (1, 3, 1)


def test_미배정_수요만_줄이면_아무것도_풀리지_않는다(seeded_database: str) -> None:
    """배정되지 않은 수요를 줄이는 것은 해제가 아니다.

    풀 것이 없는데 해제 행을 만들면 원장이 거짓이 되고, 버킷의 배정량이 음수로
    내려가 CHECK 제약에 걸린다.
    """
    request_id = place_request("EXT-1", 5)
    make_passed_stock(2)
    run_fifo()
    assert allocated_for(request_id) == 2

    revision = revise_quantities("EXT-1", {SKU_P: 3})

    assert revision.released_quantity == 0
    assert allocated_for(request_id) == 2

    with unit_of_work() as session:
        releases = session.execute(text("SELECT count(*) FROM allocation_release")).scalar_one()
    assert releases == 0


def test_감량으로_구간이_0이_되어도_행이_남는다(seeded_database: str) -> None:
    """ck_segment_quantity_not_negative가 허용하는 상태다.

    행을 지우면 allocation이 참조하던 구간이 사라지고 AC-36의 배정 이력을 잃는다.
    24-2에서 미검수 버킷이 수량 0으로 남는 것과 같은 이유다.
    """
    request_id = place_request("EXT-1", 2)
    revise_quantities("EXT-1", {SKU_P: 4})
    make_passed_stock(4)
    run_fifo()

    revise_quantities("EXT-1", {SKU_P: 2})

    # 증량 구간은 수량 0으로 남고 배정도 전부 풀렸다.
    assert _segments(request_id) == [(1, 2, 2), (2, 0, 0)]

    with unit_of_work() as session:
        segments = session.execute(text("SELECT count(*) FROM demand_segment")).scalar_one()
        # 배정 행도 남는다. 풀린 것은 released_quantity로 표현되고 행은 지워지지
        # 않는다.
        allocations = session.execute(text("SELECT count(*) FROM allocation")).scalar_one()
    assert (segments, allocations) == (2, 2)


def test_같은_수량을_다시_보내면_아무것도_바뀌지_않는다(seeded_database: str) -> None:
    """명령이 변경 후 총 수량을 담기 때문에 생기는 성질이다.

    증감분을 받는 설계였다면 재전송이 수량을 두 배로 만들었다.
    """
    request_id = place_request("EXT-1", 2)

    first = revise_quantities("EXT-1", {SKU_P: 4})
    second = revise_quantities("EXT-1", {SKU_P: 4})

    assert first.outcome is RevisionOutcome.APPLIED
    assert second.outcome is RevisionOutcome.UNCHANGED
    # 구간이 셋으로 늘지 않았다.
    assert _segments(request_id) == [(1, 2, 0), (2, 2, 0)]


def test_수량을_0으로_줄이는_변경은_거절한다(seeded_database: str) -> None:
    """NBR-06. 품목을 빼는 것은 취소 후 신규 요청이다."""
    place_request("EXT-1", 2)

    result = revise_quantities("EXT-1", {SKU_P: 0})

    assert result.outcome is RevisionOutcome.REJECTED
    assert [problem.field for problem in result.problems] == ["lines[0].quantity"]


def test_등록되지_않은_요청의_변경은_거절한다(seeded_database: str) -> None:
    """없는 요청과 바뀔 것이 없는 요청은 다른 사실이다."""
    result = revise_quantities("EXT-NONE", {SKU_P: 2})

    assert result.outcome is RevisionOutcome.REJECTED
    assert [problem.field for problem in result.problems] == ["external_request_id"]


def test_요청에_없는_품목은_거절하고_아무것도_적용하지_않는다(seeded_database: str) -> None:
    """TC-06의 성질. 일부만 적용된 상태를 유효 최신 버전으로 노출하지 않는다.

    요청에는 SKU_P만 있고 명령은 SKU_P와 SKU_Q를 함께 바꾸려 한다. 거절이
    SKU_P의 변경보다 먼저 일어나므로 아무것도 적용되지 않는다. 라인을 새로
    만들어 주지 않는 것은 그것이 SKU 구성 변경이기 때문이다(NBR-06).
    """
    request_id = place_request("EXT-1", 2)

    result = revise_quantities("EXT-1", {SKU_P: 5, SKU_Q: 3})

    assert result.outcome is RevisionOutcome.REJECTED
    assert [problem.field for problem in result.problems] == ["lines[1].sku_code"]
    assert _segments(request_id) == [(1, 2, 0)]


def test_다품목_요청의_수량을_함께_바꾼다(seeded_database: str) -> None:
    """한 명령이 바꾼 라인들은 group_id를 공유한다 (AC-36).

    나중에 "그때 그 명령이 무엇을 바꿨는가"를 되읽을 수 있어야 한다.
    """
    place_multi_line_request("EXT-1", {SKU_P: 2, SKU_Q: 3})

    result = revise_quantities("EXT-1", {SKU_P: 4, SKU_Q: 1})

    assert result.outcome is RevisionOutcome.APPLIED
    with unit_of_work() as session:
        rows = session.execute(
            text(
                "SELECT previous_quantity, new_quantity"
                " FROM request_revision ORDER BY previous_quantity"
            )
        ).all()
        groups = session.execute(
            text("SELECT count(DISTINCT group_id) FROM request_revision")
        ).scalar_one()

    assert [tuple(row) for row in rows] == [(2, 4), (3, 1)]
    # 두 라인이 한 명령으로 바뀌었으므로 묶음은 하나다.
    assert groups == 1


def test_라인_수량과_구간_수량의_합이_일치한다(seeded_database: str) -> None:
    """어긋나면 요청이 요구하는 수량과 FIFO에 걸린 수량이 달라진다.

    증량·감량을 번갈아 적용한 뒤 확인한다. 한 번만 바꾸면 우연히 맞을 수 있다.
    """
    place_request("EXT-1", 2)
    make_passed_stock(10)
    for quantity in (5, 3, 7, 4):
        revise_quantities("EXT-1", {SKU_P: quantity})
        run_fifo()

    with unit_of_work() as session:
        mismatched = session.execute(
            text(
                "SELECT count(*) FROM request_line l"
                " WHERE l.quantity <> ("
                "   SELECT coalesce(sum(s.quantity), 0) FROM demand_segment s"
                "   WHERE s.line_id = l.id)"
            )
        ).scalar_one()
    assert mismatched == 0


def test_변경_이력이_보존된다(seeded_database: str) -> None:
    """AC-36. 수요 구간과 라인 수량은 갱신되지만 변경 사실은 지워지지 않는다."""
    place_request("EXT-1", 2)
    revise_quantities("EXT-1", {SKU_P: 5}, reason="프로모션 물량 추가")
    revise_quantities("EXT-1", {SKU_P: 3}, reason="판매사 재고 조정")

    with unit_of_work() as session:
        rows = session.execute(
            text(
                "SELECT previous_quantity, new_quantity, reason"
                " FROM request_revision ORDER BY occurred_at, previous_quantity"
            )
        ).all()

    assert [tuple(row) for row in rows] == [
        (2, 5, "프로모션 물량 추가"),
        (5, 3, "판매사 재고 조정"),
    ]


def test_배정보다_많이_풀_수_없다(seeded_database: str) -> None:
    """DB 제약을 직접 시험한다.

    애플리케이션을 통과하면 잠금과 해제 계산이 막아 주므로, 그 경로로는 제약이
    실제로 걸려 있는지 알 수 없다.
    """
    place_request("EXT-1", 2)
    make_passed_stock(2)
    run_fifo()

    with pytest.raises(IntegrityError) as error:
        with unit_of_work() as session:
            session.execute(text("UPDATE allocation SET released_quantity = quantity + 1"))

    assert "ck_allocation_released_within_quantity" in str(error.value)

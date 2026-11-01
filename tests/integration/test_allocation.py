"""FIFO 배정을 실제 PostgreSQL에서 검증한다 (AC-03, AC-04).

단위 테스트가 배분 계산을 증명했다면 여기서는 그 계산이 **올바른 입력을 받는지**
증명한다. 적격 재고를 고르는 조건, 기존 배정을 빼는 뺄셈, 모듈 경계를 넘는 조합이
검증 대상이고, 어느 것도 순수 함수로는 보일 수 없다.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from fulfill.modules.inventory.domain import QC_FAIL, ReceiptCommand
from fulfill.modules.inventory.queries import find_allocatable_quantity
from fulfill.shared.db import unit_of_work
from fulfill.workflows.allocate import AllocationOutcome, summarize_request_allocation
from fulfill.workflows.receiving import ReceivingOutcome, record_receipt
from tests.scenarios import (
    NOW,
    allocated_for,
    judge_qc,
    make_passed_stock,
    place_request,
    receive_stock,
    run_fifo,
    sku_id_of,
)
from tests.seed_data import SELLER_A, SELLER_B, SKU_P, SKU_Q

pytestmark = pytest.mark.integration


def test_선순위_요청이_먼저_배정된다(seeded_database: str) -> None:
    """AC-03: 적격 재고 3개는 먼저 접수된 요청부터 채운다."""
    first = place_request("EXT-1", 2)
    second = place_request("EXT-2", 1)
    make_passed_stock(3)

    result = run_fifo()

    assert result.outcome is AllocationOutcome.APPLIED
    assert result.allocated_quantity == 3
    assert (allocated_for(first), allocated_for(second)) == (2, 1)


def test_재고가_부족하면_선순위만_부분_배정된다(seeded_database: str) -> None:
    """NBR-03과 NBR-07이 함께 걸리는 지점이다.

    후순위가 0을 받는 것(건너뛰기 금지)과 선순위가 1을 받는 것(부분 배정 허용)이
    동시에 성립해야 한다.
    """
    first = place_request("EXT-1", 2)
    second = place_request("EXT-2", 1)
    make_passed_stock(1)

    result = run_fifo()

    assert result.allocated_quantity == 1
    # 부분 배정으로 끝났다는 사실이 수치로 드러나야 AC-04를 만족한다.
    assert result.unallocated_quantity == 2
    assert result.allocatable_quantity == 0
    assert (allocated_for(first), allocated_for(second)) == (1, 0)


def test_미검수_재고는_배정되지_않는다(seeded_database: str) -> None:
    """NBR-10, AC-07. 실도착은 검수가 아니다.

    거절이 아니라 UNCHANGED다. 요청은 유효하게 대기 중이고 배정할 적격 재고가
    없을 뿐이다.
    """
    request_id = place_request("EXT-1", 2)
    receive_stock(5)

    result = run_fifo()

    assert result.outcome is AllocationOutcome.UNCHANGED
    assert (result.allocated_quantity, result.unallocated_quantity) == (0, 2)
    assert allocated_for(request_id) == 0


def test_불합격_재고는_배정되지_않는다(seeded_database: str) -> None:
    """AC-07의 위반 조항. 격리된 불량 20개를 가용으로 계산하지 않는다."""
    request_id = place_request("EXT-1", 2)
    receipt_id = receive_stock(5)
    judge_qc(receipt_id, QC_FAIL, 5)

    assert run_fifo().outcome is AllocationOutcome.UNCHANGED
    assert allocated_for(request_id) == 0


def test_귀속_미확인_입고는_배정되지_않는다(seeded_database: str) -> None:
    """AC-08. 버킷이 아예 없으므로 적격 조회에 나타나지 않는다."""
    request_id = place_request("EXT-1", 2)
    with unit_of_work() as session:
        received = record_receipt(
            session,
            ReceiptCommand(
                arrived_quantity=80,
                confirmed_by="WH-STAFF-01",
                arrived_at=NOW,
                seller_code=None,
                sku_code=None,
            ),
        )
    assert received.outcome is ReceivingOutcome.APPLIED

    assert run_fifo().outcome is AllocationOutcome.UNCHANGED
    assert allocated_for(request_id) == 0


def test_피킹_위치가_미확정이어도_배정된다(seeded_database: str) -> None:
    """NBR-12, AC-09. 위 세 테스트와 반대 방향이다.

    QC 합격 직후 버킷의 location_code는 NULL이다. 위치를 적격 조건에 넣었다면
    아무것도 배정되지 않고, 그것이 AC-09의 "정상 FIFO 배정은 허용하되"를 위반한다.
    """
    request_id = place_request("EXT-1", 2)
    make_passed_stock(2)

    with unit_of_work() as session:
        # 합격 버킷만 센다. 전량 합격 뒤에도 미검수 버킷은 수량 0으로 남으므로
        # (24-2의 버킷 설계) 전체를 세면 위치 NULL 행이 둘이다.
        unlocated = session.execute(
            text(
                "SELECT count(*) FROM stock_position"
                " WHERE qc_state = 'PASSED' AND location_code IS NULL"
            )
        ).scalar_one()
    assert unlocated == 1, "전제가 깨졌다: 합격 버킷에 위치가 붙어 있다"

    assert run_fifo().outcome is AllocationOutcome.APPLIED
    assert allocated_for(request_id) == 2


def test_같은_수량이_두_요청에_중복_배정되지_않는다(seeded_database: str) -> None:
    """AC-04, NBR-04. 각 상품 수량은 최대 한 요청에만 배정된다.

    두 요청이 각각 3개를 요구하고 재고는 3개다. 합계가 6이 되면 같은 실물을 두 번
    팔았다는 뜻이다.
    """
    first = place_request("EXT-1", 3)
    second = place_request("EXT-2", 3)
    make_passed_stock(3)

    run_fifo()

    assert allocated_for(first) + allocated_for(second) == 3
    assert (allocated_for(first), allocated_for(second)) == (3, 0)


def test_배정할_것이_없으면_재실행은_UNCHANGED다(seeded_database: str) -> None:
    """같은 SKU를 두 번 실행하는 것은 정상 요청이다.

    24-1의 재전달과 다르다. 배정에는 업무 멱등키가 없고, 두 번째 실행은 "다시
    평가해 달라"는 뜻이다. 평가했고 바뀔 것이 없으면 UNCHANGED다.
    """
    place_request("EXT-1", 3)
    make_passed_stock(3)

    assert run_fifo().outcome is AllocationOutcome.APPLIED
    second_run = run_fifo()

    assert second_run.outcome is AllocationOutcome.UNCHANGED
    assert second_run.allocated_quantity == 0
    assert second_run.unallocated_quantity == 0
    assert second_run.allocatable_quantity == 0


def test_나중에_들어온_재고는_남은_미충족분까지만_배정된다(seeded_database: str) -> None:
    """2차 배정이 수요를 초과하지 않는다 (AC-04).

    결정 2에서 밝힌 비대칭이 걸리는 지점이다. 재고 초과는 CHECK 제약이 막지만
    수요 초과는 애플리케이션이 막는다 — `segment.quantity`에서 기존 배정량을
    빼는 그 뺄셈 하나다.

    1차에서 3개를 받은 수요 5개에 재고 5개가 더 들어온다. 뺄셈이 없으면 2차에서
    5개를 더 줘서 합계 8개가 되고, 요청하지 않은 3개가 묶인다. 버킷 수량 제약은
    이것을 잡지 못한다 — 재고는 실제로 8개 있기 때문이다.
    """
    request_id = place_request("EXT-1", 5)
    make_passed_stock(3)
    assert run_fifo().allocated_quantity == 3

    make_passed_stock(5)
    second_run = run_fifo()

    assert second_run.allocated_quantity == 2
    assert second_run.unallocated_quantity == 0
    # 남은 재고 3개는 미배정으로 남는다. 요청이 5개만 필요하다.
    assert second_run.allocatable_quantity == 3
    assert allocated_for(request_id) == 5

    with unit_of_work() as session:
        ledger = session.execute(
            text("SELECT coalesce(sum(quantity), 0) FROM allocation")
        ).scalar_one()
    assert ledger == 5


def test_배정_원장의_합이_버킷의_배정량과_일치한다(seeded_database: str) -> None:
    """24-2의 "버킷 합 == 실도착 수량"에 대응하는 불변식이다.

    allocation 행은 원장이고 stock_position.allocated_quantity는 그 합계다. 중복
    상태를 의도적으로 둔 대가이므로, 두 값이 어긋나면 어느 쪽도 믿을 수 없다.

    분할 입고로 버킷이 둘인 상황에서 확인한다. 버킷이 하나면 합계와 원장이
    우연히 같아질 수 있다.
    """
    place_request("EXT-1", 7)
    make_passed_stock(3)
    make_passed_stock(5)

    run_fifo()

    with unit_of_work() as session:
        ledger = session.execute(
            text("SELECT coalesce(sum(quantity), 0) FROM allocation")
        ).scalar_one()
        totals = session.execute(
            text("SELECT coalesce(sum(allocated_quantity), 0) FROM stock_position")
        ).scalar_one()
        rows = session.execute(text("SELECT count(*) FROM allocation")).scalar_one()

    assert ledger == totals == 7
    # 두 버킷에서 각각 가져왔으므로 행이 둘이다. 하나면 한 버킷만 쓴 것이다.
    assert rows == 2


def test_적격_조회가_이미_배정된_수량을_뺀다(seeded_database: str) -> None:
    """빼지 않으면 배정된 수량이 다시 배정 가능으로 보인다."""
    place_request("EXT-1", 2)
    make_passed_stock(5)

    with unit_of_work() as session:
        before = find_allocatable_quantity(session, sku_id_of(session, SELLER_A, SKU_P))
    run_fifo()
    with unit_of_work() as session:
        after = find_allocatable_quantity(session, sku_id_of(session, SELLER_A, SKU_P))

    assert (before, after) == (5, 3)


def test_다른_SKU의_수요는_이_SKU의_재고를_가져가지_못한다(seeded_database: str) -> None:
    """SKU별 독립 FIFO(POL-002/003). 큐가 SKU 단위라는 사실의 확인이다."""
    other_sku = place_request("EXT-1", 5, sku_code=SKU_Q)
    mine = place_request("EXT-2", 5, sku_code=SKU_P)
    make_passed_stock(5, sku_code=SKU_P)

    run_fifo(sku_code=SKU_P)

    assert allocated_for(other_sku) == 0
    # 먼저 접수된 요청이지만 다른 SKU라 순서를 다투지 않는다.
    assert allocated_for(mine) == 5


def test_다른_판매사의_같은_코드_SKU와_재고가_섞이지_않는다(seeded_database: str) -> None:
    """같은 코드라도 판매사가 다르면 다른 물건이다(19 §3). 재고를 섞지 않는다."""
    theirs = place_request("EXT-1", 5, seller_code=SELLER_B)
    mine = place_request("EXT-2", 5, seller_code=SELLER_A)
    make_passed_stock(5, seller_code=SELLER_A)

    run_fifo(seller_code=SELLER_A)

    assert allocated_for(theirs) == 0
    assert allocated_for(mine) == 5


def test_등록되지_않은_판매사나_품목은_거절한다(seeded_database: str) -> None:
    """재고가 없는 것과 SKU가 없는 것은 다른 사실이다.

    둘을 모두 UNCHANGED로 답하면 운영자가 코드를 잘못 입력했는지 재고가 없는지
    구분할 수 없다.
    """
    unknown_seller = run_fifo(seller_code="SELLER-NONE")
    assert unknown_seller.outcome is AllocationOutcome.REJECTED
    assert [problem.field for problem in unknown_seller.problems] == ["seller_code"]

    unknown_sku = run_fifo(sku_code="SKU-NONE")
    assert unknown_sku.outcome is AllocationOutcome.REJECTED
    assert [problem.field for problem in unknown_sku.problems] == ["sku_code"]


def test_위치가_확정된_버킷을_먼저_배정한다(seeded_database: str) -> None:
    """업무 규칙이 아니라 동순위 사이의 선택이다(lock_allocatable_buckets 참조).

    위치 확정 작업은 24-4의 일이라 아직 명령이 없으므로, 위치를 직접 넣어 상황을
    만든다. 확인하려는 것은 적격 버킷을 고르는 순서 하나다.
    """
    request_id = place_request("EXT-1", 2)
    make_passed_stock(3)
    located = make_passed_stock(5)
    with unit_of_work() as session:
        session.execute(
            text(
                "UPDATE stock_position SET location_code = 'A-01'"
                " WHERE receipt_id = :receipt_id AND qc_state = 'PASSED'"
            ),
            {"receipt_id": located},
        )

    run_fifo()

    with unit_of_work() as session:
        locations = (
            session.execute(
                text(
                    "SELECT sp.location_code FROM allocation a"
                    " JOIN stock_position sp ON sp.id = a.stock_position_id"
                )
            )
            .scalars()
            .all()
        )

    assert allocated_for(request_id) == 2
    # 위치 미확정 버킷을 먼저 썼다면 배정은 됐지만 피킹할 수 없는 요청이 된다.
    assert locations == ["A-01"]


def test_버킷의_배정량은_수량을_넘을_수_없다(seeded_database: str) -> None:
    """DB 제약을 직접 시험한다 (결정 2).

    애플리케이션을 통과하면 잠금과 배분 계산이 막아 주므로, 그 경로로는 제약이
    실제로 걸려 있는지 알 수 없다. 우회해서 거부당하는 것을 봐야 "이중 배정을 DB가
    막는다"는 주장이 사실이 된다.
    """
    make_passed_stock(3)

    with pytest.raises(IntegrityError) as error:
        with unit_of_work() as session:
            session.execute(
                text(
                    "UPDATE stock_position SET allocated_quantity = quantity + 1"
                    " WHERE qc_state = 'PASSED'"
                )
            )

    assert "ck_stock_allocated_within_quantity" in str(error.value)


def test_없는_요청의_배정_현황은_없음으로_답한다(seeded_database: str) -> None:
    """빈 현황과 존재하지 않는 요청을 구분한다."""
    with unit_of_work() as session:
        assert summarize_request_allocation(session, uuid.uuid4()) is None

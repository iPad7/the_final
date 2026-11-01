"""FIFO 배정 경합을 실제 PostgreSQL에서 검증한다 (TC-03, TC-04, T02).

**ADR23-05 판정의 근거가 되는 파일이다.** ADR23 §4가 "같은 키 경합·없던 guard
최초 upsert·다품목 잠금/교착·timeout을 실제 PostgreSQL 테스트로 판정한다"고 남긴
숙제에 답한다.

세 가지를 따로 증명한다.

1. **잠금이 실제로 걸리는가** — lock_timeout을 짧게 걸고 막히는 것을 본다.
   통과하는 것과 잠그고 있는 것은 다른 사실이다.
2. **순서가 보존되는가** — 동시 배정에서 FIFO가 역전되지 않는다.
3. **교착이 없는가** — 정렬된 취득에서는 없고, 정렬을 뒤집으면 나타난다.
"""

import threading
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import fulfill.workflows.accept_request as intake_workflow
from fulfill.modules.allocation.guard import issue_demand_seq, lock_sku_queue
from fulfill.modules.inventory.domain import QC_PASS
from fulfill.modules.requests.domain import IntakeCommand, RequestedLine
from fulfill.shared.db import unit_of_work
from fulfill.workflows.accept_request import accept_request
from tests.concurrency.harness import WORKERS, run_together
from tests.scenarios import (
    DUE,
    NOW,
    RECEIVED_AT,
    RECIPIENT,
    allocated_for,
    judge_qc,
    make_passed_stock,
    place_request,
    receive_stock,
    revise_quantities,
    run_fifo,
    sku_id_of,
)
from tests.seed_data import SELLER_A, SKU_P, SKU_Q

pytestmark = pytest.mark.concurrency

# 경합 중 잠금을 기다리지 않고 바로 실패하게 만드는 시간. 짧아야 테스트가 빠르고,
# 0보다 커야 "순간적으로 비어 있었다"는 오해를 피한다.
LOCK_TIMEOUT = "300ms"
# 스레드 조율 대기 상한. 넘으면 테스트가 멈추는 대신 실패한다.
JOIN_TIMEOUT = 15.0

# 잠금을 잡는 두 입구. 반환값은 보지 않으므로 object로 받는다.
Acquire = Callable[[Session, uuid.UUID], object]


def _try_acquire(sku_id: uuid.UUID, acquire: Acquire) -> str:
    """짧은 lock_timeout으로 잠금을 시도한다. 막히면 'blocked'다."""
    with unit_of_work() as session:
        # SET LOCAL이므로 이 트랜잭션에만 적용된다. 전역 설정을 건드리면 다른
        # 테스트의 거동을 바꾼다.
        session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        try:
            acquire(session, sku_id)
        except OperationalError:
            return "blocked"
        return "acquired"


def _blocked_while_held(sku_id: uuid.UUID, acquire: Acquire) -> tuple[str, str]:
    """한 트랜잭션이 잠근 채 있을 때와 해제한 뒤의 결과를 각각 돌려준다.

    두 값을 함께 보는 것이 중요하다. 경합 중에 막히는 것만 확인하면 "원래 늘
    막히는" 구현도 통과한다. 해제 후 잡히는 것까지 봐야 잠금이 원인이라는 것이
    증명된다.
    """
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with unit_of_work() as session:
            acquire(session, sku_id)
            held.set()
            release.wait(timeout=JOIN_TIMEOUT)

    with ThreadPoolExecutor(max_workers=1) as pool:
        holding = pool.submit(holder)
        assert held.wait(timeout=JOIN_TIMEOUT), "잠금을 잡는 쪽이 출발하지 못했다"
        during = _try_acquire(sku_id, acquire)
        release.set()
        holding.result(timeout=JOIN_TIMEOUT)

    return during, _try_acquire(sku_id, acquire)


def test_큐_잠금이_다른_배정을_실제로_막는다(seeded_database: str) -> None:
    """ADR23-05의 전제. guard 행 잠금이 같은 SKU의 두 번째 판정을 기다리게 한다.

    이것이 성립하지 않으면 두 배정이 같은 적격 수량을 각자 자기 몫으로 계산한다.
    """
    place_request("EXT-1", 1)
    with unit_of_work() as session:
        sku_id = sku_id_of(session)

    assert _blocked_while_held(sku_id, lock_sku_queue) == ("blocked", "acquired")


def test_순번_발급도_같은_행을_잠근다(seeded_database: str) -> None:
    """결정 6의 검증.

    왕복을 3회에서 1회로 줄이면서 `INSERT … ON CONFLICT DO UPDATE … RETURNING`이
    행 배타 잠금을 잡는다고 적었다. 그것은 주장이었고 여기서 사실이 된다.

    guard 행이 이미 있는 상태에서 확인한다. 없으면 최초 삽입 경쟁이 섞여 무엇이
    막았는지 모호해진다.
    """
    place_request("EXT-1", 1)
    with unit_of_work() as session:
        sku_id = sku_id_of(session)

    assert _blocked_while_held(sku_id, issue_demand_seq) == ("blocked", "acquired")


def test_동시_배정에서도_선순위2_후순위1까지만_배정된다(seeded_database: str) -> None:
    """TC-03: 선순위2·후순위2 수요에 적격 재고 3을 동시에 배정한다.

    기대 결과가 "선순위2 + 후순위1, 합 3 이내"다. 두 실행이 각자 재고 3개를 보면
    합이 6이 되고, 그것이 NBR-04가 금지하는 이중 배정이다.
    """
    first = place_request("EXT-1", 2)
    second = place_request("EXT-2", 2)
    make_passed_stock(3)

    results = run_together([run_fifo, run_fifo])

    assert sum(result.allocated_quantity for result in results) == 3
    assert (allocated_for(first), allocated_for(second)) == (2, 1)


def test_여러_배정이_동시에_실행돼도_적격_수량을_넘지_않는다(seeded_database: str) -> None:
    """T02: 총 배정 ≤ 적격 미배정 실물, 중복 배정·FIFO 역전 없음.

    수요 12개(3개씩 4건)에 재고는 5개뿐이다. FIFO가 지켜지면 분배가 하나로
    정해진다 — 3, 2, 0, 0. 동시 실행이 순서를 흐트러뜨리면 이 값이 달라진다.
    """
    requests = [place_request(f"EXT-{index}", 3) for index in range(WORKERS)]
    make_passed_stock(5)

    results = run_together([run_fifo] * WORKERS)

    assert sum(result.allocated_quantity for result in results) == 5
    assert [allocated_for(request_id) for request_id in requests] == [3, 2, 0, 0]

    with unit_of_work() as session:
        ledger = session.execute(
            text("SELECT coalesce(sum(quantity), 0) FROM allocation")
        ).scalar_one()
        reserved = session.execute(
            text("SELECT coalesce(sum(allocated_quantity), 0) FROM stock_position")
        ).scalar_one()
    assert ledger == reserved == 5


def test_검수_합격과_배정이_동시에_일어나도_수량이_보존된다(seeded_database: str) -> None:
    """검수는 버킷 사이 수량 이동이고 배정은 그 버킷을 예약한다. 둘이 부딪힌다.

    어느 쪽이 먼저인지는 정해지지 않는다. 배정이 먼저면 적격 재고가 없어 0개,
    검수가 먼저면 10개다. 둘 다 정상이므로 결과값을 단정하지 않는다.

    단정하는 것은 불변식이다. 24-2의 "버킷 수량 합 == 실도착 수량"과 24-3의
    "배정 원장 합 == 버킷 배정량 합"이 어떤 순서에서도 유지되어야 한다. 그리고
    다시 실행하면 반드시 전량 배정된다 — 경합이 수량을 잃지 않았다는 뜻이다.
    """
    request_id = place_request("EXT-1", 10)
    receipt_id = receive_stock(10)

    def judge() -> str:
        judge_qc(receipt_id, QC_PASS, 10)
        return "judged"

    def allocate() -> str:
        run_fifo()
        return "allocated"

    assert sorted(run_together([judge, allocate])) == ["allocated", "judged"]

    with unit_of_work() as session:
        bucket_total = session.execute(
            text("SELECT coalesce(sum(quantity), 0) FROM stock_position WHERE receipt_id = :id"),
            {"id": receipt_id},
        ).scalar_one()
        reserved = session.execute(
            text("SELECT coalesce(sum(allocated_quantity), 0) FROM stock_position")
        ).scalar_one()
        ledger = session.execute(
            text("SELECT coalesce(sum(quantity), 0) FROM allocation")
        ).scalar_one()

    assert bucket_total == 10, "검수가 수량을 만들거나 없앴다"
    assert ledger == reserved

    run_fifo()
    assert allocated_for(request_id) == 10


def _multi_line_command(external_id: str, sku_codes: tuple[str, str]) -> IntakeCommand:
    """두 SKU를 담은 요청. 라인 순서가 잠금 취득 순서의 입력이 된다."""
    return IntakeCommand(
        seller_code=SELLER_A,
        external_request_id=external_id,
        lines=tuple(RequestedLine(sku_code=code, quantity=1) for code in sku_codes),
        recipient=RECIPIENT,
        initial_due_at=DUE,
        desired_ship_date=None,
        occurred_at=NOW,
    )


def _attempt_intake(command: IntakeCommand) -> str:
    """접수를 시도하고 교착 여부만 돌려준다.

    예외를 그대로 올리면 경합 하네스가 바로 실패하므로, 두 스레드의 결과를 함께
    보기 위해 값으로 바꾼다.
    """
    try:
        with unit_of_work() as session:
            accept_request(session, command, RECEIVED_AT)
    except OperationalError as error:
        return "deadlock" if "deadlock" in str(error).lower() else "error"
    return "ok"


def test_다품목_요청이_반대_순서로_와도_교착하지_않는다(seeded_database: str) -> None:
    """24-1의 `sorted(set(sku_ids))` 한 줄이 하는 일이다.

    두 요청이 (P, Q)와 (Q, P) 순서로 같은 두 SKU를 요구한다. 정렬하면 둘 다 P를
    먼저 잡으려 하므로 한쪽이 P에서 기다리고, 기다리는 쪽은 아무 잠금도 쥐고 있지
    않다. 사이클이 생길 수 없으므로 교착은 구조적으로 불가능하다.
    """
    tasks = [
        lambda: _attempt_intake(_multi_line_command("EXT-1", (SKU_P, SKU_Q))),
        lambda: _attempt_intake(_multi_line_command("EXT-2", (SKU_Q, SKU_P))),
    ]

    assert run_together(tasks) == ["ok", "ok"]


def test_정렬하지_않고_잠그면_교착이_나타난다(
    seeded_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """앞 테스트와 한 쌍이다. 정렬이 무엇을 막고 있었는지 보여 준다.

    정렬을 빼고, 두 스레드가 **각자 첫 잠금을 쥔 뒤에** 두 번째로 넘어가도록
    장벽으로 맞춘다. 우연히 부딪히기를 기대하지 않는다 — 그러면 테스트가 어떤
    날은 통과하고 어떤 날은 실패한다.

    결과는 PostgreSQL이 한쪽을 희생자로 고르는 것이다. 정확히 하나만 교착으로
    끝나야 하며, 둘 다 실패하거나 둘 다 성공하면 전제가 틀린 것이다.
    """
    both_hold_first_lock = threading.Barrier(2)

    def unsorted_issue(session: Session, sku_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, int]:
        issued: dict[uuid.UUID, int] = {}
        for index, sku_id in enumerate(sku_ids):
            if index == 1:
                # 상대도 첫 잠금을 쥘 때까지 기다린다. 이 지점을 지나면 두 스레드가
                # 서로가 쥔 행을 요구하므로 사이클이 완성된다.
                both_hold_first_lock.wait(timeout=JOIN_TIMEOUT)
            issued[sku_id] = issue_demand_seq(session, sku_id)
        return issued

    monkeypatch.setattr(intake_workflow, "issue_demand_seqs", unsorted_issue)

    tasks = [
        lambda: _attempt_intake(_multi_line_command("EXT-1", (SKU_P, SKU_Q))),
        lambda: _attempt_intake(_multi_line_command("EXT-2", (SKU_Q, SKU_P))),
    ]

    assert sorted(run_together(tasks)) == ["deadlock", "ok"]


def test_수량_감소와_배정이_동시에_일어나도_판정이_일관된다(seeded_database: str) -> None:
    """TC-05: 정당한 수량 감소와 같은 판매사·SKU의 재배정이 경합한다.

    적격 재고 5개가 FR-1에 2개, FR-2에 3개 배정되고 FR-3은 아무것도 받지 못한
    상태에서, FR-1을 1개로 줄이는 명령과 배정 실행이 동시에 출발한다.

    **최종 상태가 순서와 무관해야 한다.** 감량이 먼저면 풀린 1개를 그 자리에서
    FR-3에 주고, 배정이 먼저면 줄 것이 없어 아무것도 하지 않은 뒤 감량이 FR-3에
    준다. 어느 쪽이든 FR-1 1 · FR-2 3 · FR-3 1이다. 결과가 갈리면 TX-03의
    "순서 경합은 일관된 판정"이 깨진 것이다.

    FR-2의 3개가 그대로인 것이 NBR-04의 금지 조항이다.
    """
    first = place_request("EXT-1", 2)
    second = place_request("EXT-2", 3)
    third = place_request("EXT-3", 2)
    make_passed_stock(5)
    run_fifo()
    assert (allocated_for(first), allocated_for(second), allocated_for(third)) == (2, 3, 0)

    def shrink() -> str:
        revise_quantities("EXT-1", {SKU_P: 1})
        return "revised"

    def allocate() -> str:
        run_fifo()
        return "allocated"

    assert sorted(run_together([shrink, allocate])) == ["allocated", "revised"]

    assert (allocated_for(first), allocated_for(second), allocated_for(third)) == (1, 3, 1)

    with unit_of_work() as session:
        reserved = session.execute(
            text("SELECT coalesce(sum(allocated_quantity), 0) FROM stock_position")
        ).scalar_one()
        active = session.execute(
            text("SELECT coalesce(sum(quantity - released_quantity), 0) FROM allocation")
        ).scalar_one()
    assert reserved == active == 5


def test_같은_요청에_수량_변경이_동시에_오면_하나씩_적용된다(seeded_database: str) -> None:
    """guard가 수량 변경도 직렬화한다.

    두 변경이 끼어들면 둘 다 같은 현재 수량을 읽고 각자 증량 구간을 만든다.
    그러면 라인 수량과 구간 수량의 합이 어긋난다 — 그 불변식으로 판정한다.

    어느 쪽이 마지막인지는 정해지지 않으므로 최종 수량을 단정하지 않는다. 단정하는
    것은 "둘 중 하나의 값이고, 구간 합이 그 값과 같다"는 것이다.
    """
    place_request("EXT-1", 2)

    def revise_to(quantity: int) -> Callable[[], str]:
        def run() -> str:
            revise_quantities("EXT-1", {SKU_P: quantity})
            return f"revised-{quantity}"

        return run

    run_together([revise_to(4), revise_to(6)])

    with unit_of_work() as session:
        line_quantity = session.execute(text("SELECT quantity FROM request_line")).scalar_one()
        segment_total = session.execute(
            text("SELECT coalesce(sum(quantity), 0) FROM demand_segment")
        ).scalar_one()

    assert line_quantity in (4, 6)
    assert segment_total == line_quantity

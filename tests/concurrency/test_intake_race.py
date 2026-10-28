"""동시 접수 경합을 실제 PostgreSQL에서 검증한다 (TC-01, TC-02).

여기서 쓰는 스레드는 각자 자기 커넥션으로 자기 트랜잭션을 돈다. SQLite로는 이
상황을 만들 수 없고, 애플리케이션 조회만으로는 막을 수 없는 구간이 바로 이
구간이다.
"""

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

import fulfill.workflows.accept_request as workflow
from fulfill.modules.requests.domain import IntakeCommand
from fulfill.modules.requests.repository import ExistingRequest
from fulfill.shared.db import unit_of_work
from fulfill.workflows.accept_request import IntakeOutcome, IntakeResult, accept_request

pytestmark = pytest.mark.concurrency

RECEIVED_AT = datetime(2026, 10, 28, 1, 30, tzinfo=UTC)
WORKERS = 4


def _run_together(tasks: list[Callable[[], IntakeResult]]) -> list[IntakeResult]:
    """모든 작업을 같은 순간에 출발시킨다.

    출발을 맞추지 않으면 순차 실행이 되어 경합이 일어나지 않고, 테스트가 허위로
    통과한다.
    """
    barrier = threading.Barrier(len(tasks))

    def run(task: Callable[[], IntakeResult]) -> IntakeResult:
        barrier.wait()
        return task()

    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        return [future.result() for future in [pool.submit(run, t) for t in tasks]]


def test_같은_요청을_동시에_여러_번_보내도_하나만_생긴다(
    seeded_database: str, base_command: IntakeCommand
) -> None:
    """TC-01: FR 1개, 수요 1회. 나머지는 기존 결과를 돌려받는다."""

    def accept() -> IntakeResult:
        with unit_of_work() as session:
            return accept_request(session, base_command, RECEIVED_AT)

    results = _run_together([accept] * WORKERS)

    outcomes = [result.outcome for result in results]
    assert outcomes.count(IntakeOutcome.APPLIED) == 1
    assert outcomes.count(IntakeOutcome.UNCHANGED) == WORKERS - 1
    # 패배한 쪽도 같은 요청 id를 돌려받아야 재전송이 멱등해진다.
    assert len({result.request_id for result in results}) == 1

    with unit_of_work() as session:
        requests = session.execute(text("SELECT count(*) FROM fulfillment_request")).scalar_one()
        segments = session.execute(text("SELECT count(*) FROM demand_segment")).scalar_one()
    assert (requests, segments) == (1, 2)


def test_조회_후_삽입_사이에_끼어든_요청은_기존_결과로_수습된다(
    seeded_database: str, base_command: IntakeCommand, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-01 복구 경로를 결정론적으로 지나게 만든다.

    스레드를 늘려 우연히 부딪히기를 기대하지 않는다. 조회가 None을 돌려준 직후
    다른 커넥션이 같은 요청을 커밋하게 해서, 우리 INSERT가 반드시 유니크 위반을
    맞도록 만든다.

    이 테스트는 READ COMMITTED에 의존한다. 위반 뒤 재조회가 상대의 커밋된 행을
    볼 수 있어야 수습이 성립한다.
    """
    original_find = workflow.find_existing
    intruded = threading.Event()

    def find_then_let_other_commit(
        session: Session, seller_id: uuid.UUID, external_request_id: str
    ) -> ExistingRequest | None:
        found = original_find(session, seller_id, external_request_id)
        if found is None and not intruded.is_set():
            # 재귀를 막으려고 깃발을 먼저 세운다. 아래 호출도 이 훅을 지난다.
            intruded.set()
            with unit_of_work() as other_session:
                accept_request(other_session, base_command, RECEIVED_AT)
        return found

    monkeypatch.setattr(workflow, "find_existing", find_then_let_other_commit)

    with unit_of_work() as session:
        result = accept_request(session, base_command, RECEIVED_AT)

    assert intruded.is_set(), "끼어들기가 실제로 일어나지 않았다"
    assert result.outcome is IntakeOutcome.UNCHANGED

    with unit_of_work() as session:
        requests = session.execute(text("SELECT count(*) FROM fulfillment_request")).scalar_one()
        segments = session.execute(text("SELECT count(*) FROM demand_segment")).scalar_one()
    assert (requests, segments) == (1, 2)


def test_끼어든_요청의_내용이_다르면_충돌로_끝난다(
    seeded_database: str, base_command: IntakeCommand, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-02: 같은 ID에 다른 내용이 동시에 와도 FR이 둘 생기지 않는다."""
    original_find = workflow.find_existing
    intruder = replace(
        base_command,
        recipient=replace(base_command.recipient, address="부산시 다른곳 202"),
    )
    intruded = threading.Event()

    def find_then_let_other_commit(
        session: Session, seller_id: uuid.UUID, external_request_id: str
    ) -> ExistingRequest | None:
        found = original_find(session, seller_id, external_request_id)
        if found is None and not intruded.is_set():
            intruded.set()
            with unit_of_work() as other_session:
                accept_request(other_session, intruder, RECEIVED_AT)
        return found

    monkeypatch.setattr(workflow, "find_existing", find_then_let_other_commit)

    with unit_of_work() as session:
        result = accept_request(session, base_command, RECEIVED_AT)

    assert intruded.is_set()
    assert result.outcome is IntakeOutcome.CONFLICT

    with unit_of_work() as session:
        requests = session.execute(text("SELECT count(*) FROM fulfillment_request")).scalar_one()
        stored = session.execute(
            text("SELECT recipient_address FROM fulfillment_request")
        ).scalar_one()
    assert requests == 1
    # 먼저 커밋한 쪽이 남는다. 뒤에 온 내용으로 덮지 않는다.
    assert stored == "부산시 다른곳 202"


def test_같은_SKU에_동시_수요가_생겨도_순번이_중복되거나_빠지지_않는다(
    seeded_database: str, base_command: IntakeCommand
) -> None:
    """guard 설계 검증: 발급 순번이 커밋 순서대로 하나씩 나간다.

    DB sequence였다면 롤백 때 결번이 생기고 커밋 순서도 보장되지 않는다.
    """
    single_line = replace(base_command, lines=(base_command.lines[0],))

    def accept(index: int) -> Callable[[], IntakeResult]:
        command = replace(single_line, external_request_id=f"EXT-{index}")

        def run() -> IntakeResult:
            with unit_of_work() as session:
                return accept_request(session, command, RECEIVED_AT)

        return run

    results = _run_together([accept(index) for index in range(WORKERS)])

    assert all(result.outcome is IntakeOutcome.APPLIED for result in results)

    with unit_of_work() as session:
        seqs = [
            row[0]
            for row in session.execute(text("SELECT seq FROM demand_segment ORDER BY seq")).all()
        ]
        next_seq = session.execute(
            text("SELECT next_demand_seq FROM sku_demand_guard")
        ).scalar_one()

    assert seqs == list(range(1, WORKERS + 1))
    assert next_seq == WORKERS + 1

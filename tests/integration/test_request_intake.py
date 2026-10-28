"""출고 요청 접수를 실제 PostgreSQL에서 검증한다 (AC-01, AC-02).

판정이 DB 제약과 맞물려 있어 SQLite로는 보일 수 없다. 유니크 제약이 동시 접수를
막는다는 사실 자체가 검증 대상이기 때문이다.
"""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from fulfill.modules.requests.domain import IntakeCommand, RequestedLine
from fulfill.shared.db import unit_of_work
from fulfill.workflows.accept_request import IntakeOutcome, accept_request
from tests.seed_data import SELLER_B, SKU_P

pytestmark = pytest.mark.integration

RECEIVED_AT = datetime(2026, 10, 28, 1, 30, tzinfo=UTC)


def _row_counts(session: Session) -> tuple[int, int]:
    """요청 수와 수요 구간 수.

    재전달이 아무것도 늘리지 않았다는 것이 AC-02의 핵심 관찰이다.
    """
    requests = session.execute(text("SELECT count(*) FROM fulfillment_request")).scalar_one()
    segments = session.execute(text("SELECT count(*) FROM demand_segment")).scalar_one()
    return int(requests), int(segments)


def test_재고가_없어도_요청을_접수한다(seeded_database: str, base_command: IntakeCommand) -> None:
    """AC-01: 재고 부족만을 이유로 거절하지 않는다."""
    with unit_of_work() as session:
        result = accept_request(session, base_command, RECEIVED_AT)

    assert result.outcome is IntakeOutcome.APPLIED
    assert result.request_id is not None

    with unit_of_work() as session:
        # 재고 행이 하나도 없는 상태였다.
        assert _row_counts(session) == (1, 2)


def test_동일_내용_재전달은_기존_결과를_돌려준다(
    seeded_database: str, base_command: IntakeCommand
) -> None:
    """AC-02: 같은 ID·같은 내용은 신규 작업이나 수요를 만들지 않는다."""
    with unit_of_work() as session:
        first = accept_request(session, base_command, RECEIVED_AT)

    # 공백만 다른 같은 내용으로 재전달한다.
    resent = replace(
        base_command,
        recipient=replace(base_command.recipient, address="  서울시   어딘가 101 "),
    )
    with unit_of_work() as session:
        second = accept_request(session, resent, RECEIVED_AT)

    assert second.outcome is IntakeOutcome.UNCHANGED
    assert second.request_id == first.request_id

    with unit_of_work() as session:
        # FR만 재사용하고 수요를 또 만들면 같은 요청이 FIFO에 두 번 서게 된다.
        assert _row_counts(session) == (1, 2)


def test_같은_ID에_다른_내용이_오면_충돌로_거절한다(
    seeded_database: str, base_command: IntakeCommand
) -> None:
    """AC-02: 상이 내용은 명시적 충돌이며 기존 요청을 바꾸지 않는다."""
    with unit_of_work() as session:
        first = accept_request(session, base_command, RECEIVED_AT)

    moved = replace(
        base_command,
        recipient=replace(base_command.recipient, address="부산시 다른곳 202"),
    )
    with unit_of_work() as session:
        second = accept_request(session, moved, RECEIVED_AT)

    assert second.outcome is IntakeOutcome.CONFLICT
    # 어느 요청과 충돌했는지 알려줘야 판매사가 정정 경로를 찾을 수 있다.
    assert second.request_id == first.request_id

    with unit_of_work() as session:
        assert _row_counts(session) == (1, 2)
        stored = session.execute(
            text("SELECT recipient_address FROM fulfillment_request")
        ).scalar_one()
    assert stored == "서울시 어딘가 101"


def test_다른_판매사의_같은_ID는_별개_요청이다(
    seeded_database: str, base_command: IntakeCommand
) -> None:
    """AC-02 위반 방지: 외부 요청 ID의 유일 범위는 판매사 안쪽이다."""
    with unit_of_work() as session:
        from_a = accept_request(session, base_command, RECEIVED_AT)

    # 판매사만 바꾸고 외부 요청 ID는 그대로 둔다.
    from_seller_b = replace(base_command, seller_code=SELLER_B)
    with unit_of_work() as session:
        from_b = accept_request(session, from_seller_b, RECEIVED_AT)

    assert from_b.outcome is IntakeOutcome.APPLIED
    assert from_b.request_id != from_a.request_id

    with unit_of_work() as session:
        assert _row_counts(session) == (2, 4)


def test_등록되지_않은_판매사는_거절한다(seeded_database: str, base_command: IntakeCommand) -> None:
    """AC-01: 거절 사유를 확인할 수 있어야 한다."""
    unknown = replace(base_command, seller_code="SELLER-NOPE")

    with unit_of_work() as session:
        result = accept_request(session, unknown, RECEIVED_AT)

    assert result.outcome is IntakeOutcome.REJECTED
    assert [problem.field for problem in result.problems] == ["seller_code"]

    with unit_of_work() as session:
        assert _row_counts(session) == (0, 0)


def test_등록되지_않은_SKU는_해당_라인을_지목해_거절한다(
    seeded_database: str, base_command: IntakeCommand
) -> None:
    """AC-01: 어느 라인이 문제인지 알 수 있어야 한다."""
    unknown_sku = replace(
        base_command, lines=(RequestedLine("SKU-NOPE", 1), RequestedLine(SKU_P, 2))
    )

    with unit_of_work() as session:
        result = accept_request(session, unknown_sku, RECEIVED_AT)

    assert result.outcome is IntakeOutcome.REJECTED
    assert [problem.field for problem in result.problems] == ["lines[0].sku_code"]

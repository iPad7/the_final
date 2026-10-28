"""요청 접수 도메인 로직 — 정규화·검증·fingerprint (AC-01, AC-02).

DB가 필요 없다. 도메인이 SQLAlchemy를 모르기 때문이다. 기준 명령은 conftest의
`base_command` 픽스처에 있고, 각 테스트는 거기서 바꿀 부분만 바꾼다.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

from fulfill.modules.requests.domain import (
    MAX_EXTERNAL_REQUEST_ID_LENGTH,
    IntakeCommand,
    RequestedLine,
    fingerprint,
    validate,
)

KST = timezone(timedelta(hours=9))


def test_라인_순서가_달라도_같은_요청이다(base_command: IntakeCommand) -> None:
    reordered = replace(base_command, lines=tuple(reversed(base_command.lines)))

    assert fingerprint(reordered) == fingerprint(base_command)


def test_공백_차이는_같은_요청이다(base_command: IntakeCommand) -> None:
    spaced = replace(
        base_command, recipient=replace(base_command.recipient, address="  서울시   어딘가 101  ")
    )

    assert fingerprint(spaced) == fingerprint(base_command)


def test_같은_시점을_다른_시간대로_써도_같은_요청이다(base_command: IntakeCommand) -> None:
    # 2026-11-05 09:00 KST == 2026-11-05 00:00 UTC
    utc_form = replace(base_command, initial_due_at=datetime(2026, 11, 5, 0, tzinfo=UTC))

    assert fingerprint(utc_form) == fingerprint(base_command)


def test_주소가_다르면_다른_요청이다(base_command: IntakeCommand) -> None:
    # 여기서 같다고 판정되면 재전달로 오인해 엉뚱한 곳으로 출고된다.
    moved = replace(
        base_command, recipient=replace(base_command.recipient, address="부산시 다른곳 202")
    )

    assert fingerprint(moved) != fingerprint(base_command)


def test_수량이_다르면_다른_요청이다(base_command: IntakeCommand) -> None:
    more = replace(base_command, lines=(RequestedLine("SKU-P", 6), RequestedLine("SKU-Q", 2)))

    assert fingerprint(more) != fingerprint(base_command)


def test_발행_시각이_달라도_같은_요청이다(base_command: IntakeCommand) -> None:
    # 재전송이 새 발행 시각을 달고 올 수 있다. 포함하면 모든 재전송이 충돌이 된다.
    resent = replace(base_command, occurred_at=datetime(2026, 10, 28, 11, tzinfo=KST))

    assert fingerprint(resent) == fingerprint(base_command)


def test_정상_요청은_재고와_무관하게_통과한다(base_command: IntakeCommand) -> None:
    # 도메인은 재고를 조회하지 않는다. 재고 없는 요청도 유효하게 접수되어 대기한다.
    assert validate(base_command) == ()


def test_수량이_0이하면_거절한다(base_command: IntakeCommand) -> None:
    zero = replace(base_command, lines=(RequestedLine("SKU-P", 0),))

    fields = [problem.field for problem in validate(zero)]

    assert "lines[0].quantity" in fields


def test_같은_SKU가_중복된_라인은_거절한다(base_command: IntakeCommand) -> None:
    # 중복되면 FIFO 순번이 어느 라인에 속하는지 모호해진다.
    duplicated = replace(base_command, lines=(RequestedLine("SKU-P", 1), RequestedLine("SKU-P", 2)))

    fields = [problem.field for problem in validate(duplicated)]

    assert "lines[1].sku_code" in fields


def test_시간대가_없는_기한은_거절한다(base_command: IntakeCommand) -> None:
    naive = replace(base_command, initial_due_at=datetime(2026, 11, 5, 9))

    fields = [problem.field for problem in validate(naive)]

    assert "initial_due_at" in fields


def test_외부_요청_ID가_상한을_넘으면_거절한다(base_command: IntakeCommand) -> None:
    # 상한이 없으면 검증을 통과한 뒤 INSERT에서 DB 오류가 되고, 판매사는 읽을 수
    # 있는 사유 대신 서버 오류를 받는다.
    too_long = replace(base_command, external_request_id="X" * (MAX_EXTERNAL_REQUEST_ID_LENGTH + 1))

    fields = [problem.field for problem in validate(too_long)]

    assert "external_request_id" in fields


def test_상한_길이는_그대로_통과한다(base_command: IntakeCommand) -> None:
    exactly = replace(base_command, external_request_id="X" * MAX_EXTERNAL_REQUEST_ID_LENGTH)

    assert validate(exactly) == ()

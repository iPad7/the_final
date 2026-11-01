"""요청 접수 도메인 로직 — 정규화·검증·fingerprint (AC-01, AC-02).

DB가 필요 없다. 도메인이 SQLAlchemy를 모르기 때문이다. 기준 명령은 conftest의
`base_command` 픽스처에 있고, 각 테스트는 거기서 바꿀 부분만 바꾼다.
"""

import uuid
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

from fulfill.modules.requests.domain import (
    MAX_EXTERNAL_REQUEST_ID_LENGTH,
    IntakeCommand,
    RequestedLine,
    ReviseCommand,
    ReviseLine,
    SegmentQuantity,
    SegmentReduction,
    fingerprint,
    plan_demand_reduction,
    validate,
    validate_revision,
)
from fulfill.shared.validation import ValidationProblem

KST = timezone(timedelta(hours=9))


def _segment_id(suffix: int) -> uuid.UUID:
    """읽기 쉬운 고정 id. 실패 메시지에서 어느 구간인지 바로 보인다."""
    return uuid.UUID(f"00000000-0000-0000-0000-{suffix:012d}")


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


# 수량 변경 명령 — AC-03의 "FR-1이 2개 추가했다"가 오는 입구다.
REVISION = ReviseCommand(
    seller_code="SELLER-A",
    external_request_id="EXT-1",
    lines=(ReviseLine(sku_code="SKU-P", quantity=4),),
    revised_by="SELLER-A-STAFF-01",
    reason="판매사 재고 조정 요청",
    occurred_at=datetime(2026, 11, 1, 9, tzinfo=UTC),
)


def _fields(problems: Iterable[ValidationProblem]) -> set[str]:
    return {problem.field for problem in problems}


def test_수량_변경_명령은_유효하다() -> None:
    assert validate_revision(REVISION) == ()


def test_수량을_0으로_줄이는_변경은_거절한다() -> None:
    # NBR-06. 0은 감량이 아니라 SKU 구성 변경이고, 취소 후 신규 요청이어야 한다.
    # 허용하면 라인을 0으로 만들어 구성을 바꾸는 우회 경로가 생긴다.
    zeroed = replace(REVISION, lines=(ReviseLine(sku_code="SKU-P", quantity=0),))

    assert _fields(validate_revision(zeroed)) == {"lines[0].quantity"}


def test_변경_사유가_필요하다() -> None:
    # AC-03의 업무 근거에 "감소 사유"가 있다. 사유 없는 변경은 빈칸이다.
    assert _fields(validate_revision(replace(REVISION, reason="   "))) == {"reason"}


def test_변경_담당자가_필요하다() -> None:
    assert _fields(validate_revision(replace(REVISION, revised_by=""))) == {"revised_by"}


def test_같은_품목을_두_번_지정하면_거절한다() -> None:
    # 어느 수량이 최종인지 정해지지 않는다. uq_line_request_sku 때문에 라인도
    # SKU당 하나다.
    duplicated = replace(
        REVISION,
        lines=(ReviseLine("SKU-P", 4), ReviseLine(" SKU-P ", 2)),
    )

    assert "lines" in _fields(validate_revision(duplicated))


def test_변경할_품목이_없으면_거절한다() -> None:
    assert "lines" in _fields(validate_revision(replace(REVISION, lines=())))


def test_변경_시각에_시간대가_있어야_한다() -> None:
    naive = replace(REVISION, occurred_at=datetime(2026, 11, 1, 9))

    assert _fields(validate_revision(naive)) == {"occurred_at"}


# 원수요 2 + 증량 2 = 4인 라인. 감량은 순번 2번 구간부터 깎여야 한다.
ORIGINAL = SegmentQuantity(segment_id=_segment_id(1), seq=1, quantity=2)
INCREASE = SegmentQuantity(segment_id=_segment_id(2), seq=2, quantity=2)


def test_감량은_최신_구간부터_줄인다() -> None:
    # D-01, POL-004. 증량 배분이 순번 오름차순인 것과 정확히 반대다.
    plan = plan_demand_reduction((ORIGINAL, INCREASE), shortfall=1)

    assert plan == (SegmentReduction(segment_id=_segment_id(2), quantity=1),)


def test_최신_구간을_다_쓰면_그_앞_구간으로_넘어간다() -> None:
    # 4개를 3개 줄이면 증량 2개가 전부 사라지고 원수요에서 1개가 더 줄어든다.
    plan = plan_demand_reduction((ORIGINAL, INCREASE), shortfall=3)

    assert plan == (
        SegmentReduction(segment_id=_segment_id(2), quantity=2),
        SegmentReduction(segment_id=_segment_id(1), quantity=1),
    )


def test_오래된_구간을_먼저_줄이지_않는다() -> None:
    # 입력 순서를 뒤집어도 결과가 같아야 한다. 정렬을 호출자에게 맡겼다면 여기서
    # 깨진다.
    plan = plan_demand_reduction((INCREASE, ORIGINAL), shortfall=1)

    assert plan == (SegmentReduction(segment_id=_segment_id(2), quantity=1),)


def test_이미_0으로_줄어든_구간은_지나간다() -> None:
    # 구간 행은 이력으로 남으므로 수량 0인 구간이 존재한다. 그것을 줄이려 들면
    # 수량 0인 감량 행이 생긴다.
    spent = SegmentQuantity(segment_id=_segment_id(3), seq=3, quantity=0)
    plan = plan_demand_reduction((ORIGINAL, INCREASE, spent), shortfall=1)

    assert plan == (SegmentReduction(segment_id=_segment_id(2), quantity=1),)


def test_줄일_것이_없으면_빈_계획이_나온다() -> None:
    assert plan_demand_reduction((ORIGINAL, INCREASE), shortfall=0) == ()

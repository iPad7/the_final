"""피킹 위치 확인의 조정 계층 (AC-09).

NBR-12는 "QC 합격 후 피킹 위치 미확정 상품은 배정 가능하지만 피킹 불가"를
요구한다. 24-2는 그 상태를 표현하는 데까지만 갔다 — 합격 버킷의 location_code가
NULL이면 미확정이라는 구조를 만들었지만, NULL을 벗어나는 사건이 없었다. 이
모듈이 그 사건이다.

여기서 만드는 것은 위치 확인이라는 **사실**이고, 그 사실을 관문으로 쓰는 판정은
3단계의 일이다. 위치가 확정돼도 전량 배정과 HOLD 없음이 함께 성립해야 피킹할 수
있다(AC-05).
"""

import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy.orm import Session

from fulfill.modules.inventory.domain import ConfirmLocationCommand, validate_confirm_location
from fulfill.modules.inventory.queries import receipt_exists
from fulfill.modules.inventory.repository import confirm_bucket_location, lock_passed_buckets
from fulfill.shared.validation import ValidationProblem, normalize_text


class LocationOutcome(StrEnum):
    """위치 확인 명령의 판정 결과.

    같은 위치로 다시 확인하는 것이 UNCHANGED다. 현장 확인 보고가 두 번 올라오는
    일은 사고가 아니며, 거절로 만들면 담당자가 자기 입력을 의심하게 된다.
    """

    # 미확정이던 합격 재고의 피킹 위치를 확정했다.
    APPLIED = "APPLIED"
    # 이미 그 위치로 확인돼 있었다.
    UNCHANGED = "UNCHANGED"
    # 명령이 유효하지 않거나, 확정할 합격 재고가 없거나, 재배치 요청이다.
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class LocationResult:
    """위치 확인 판정과 그 대상."""

    outcome: LocationOutcome
    # 위치가 확정된 버킷. UNCHANGED·REJECTED에서는 None이다.
    bucket_id: uuid.UUID | None = None
    # 그 버킷의 수량. AC-09의 "작업 가능성 판단"에 쓸 근거다.
    confirmed_quantity: int = 0
    problems: tuple[ValidationProblem, ...] = field(default_factory=tuple)


def _rejected(field_name: str, reason: str) -> LocationResult:
    """사유 하나로 거절한다."""
    return LocationResult(
        LocationOutcome.REJECTED, problems=(ValidationProblem(field_name, reason),)
    )


def confirm_picking_location(session: Session, command: ConfirmLocationCommand) -> LocationResult:
    """합격 재고의 실제 피킹 위치를 확정한다 (AC-09).

    위치 미확정 합격 버킷은 최대 하나다. uq_stock_bucket이 NULLS NOT DISTINCT라서
    `(sku, receipt, PASSED, NULL)`이 유일하기 때문이며, 24-2의 그 결정 덕분에
    "찾은 것이 하나임"을 따로 보장할 필요가 없다.

    거절 두 종류는 의도된 범위 경계다.

    **병합 필요:** 요청한 위치에 같은 입고묶음의 다른 합격 버킷이 이미 있다. 부분
    검수로 합격 버킷이 둘로 갈라진 뒤 둘을 같은 자리로 합치려는 경우이며, 수량과
    배정 합계를 옮기고 allocation이 가리키는 버킷을 재지정해야 한다. 어느 배정이
    어느 버킷을 따라가는지는 어떤 문서도 정하지 않았으므로 임의로 정하지 않는다.

    **재배치:** 이미 다른 위치로 확인된 재고를 옮기려는 경우다. 실물 이동이 선행
    되는 별도 업무이고 AC-09의 범위가 아니다.
    """
    problems = validate_confirm_location(command)
    if problems:
        return LocationResult(LocationOutcome.REJECTED, problems=problems)

    if not receipt_exists(session, command.receipt_id):
        return _rejected("receipt_id", "등록되지 않은 입고묶음입니다")

    buckets = lock_passed_buckets(session, command.receipt_id)
    if not buckets:
        # 미검수·불합격만 있거나 귀속 미확정이다. AC-09는 합격품의 위치를 다루므로
        # 확정할 대상이 없다.
        return _rejected("receipt_id", "위치를 확정할 합격 재고가 없습니다")

    location_code = normalize_text(command.location_code)
    already_there = any(bucket.location_code == location_code for bucket in buckets)
    unconfirmed = next((bucket for bucket in buckets if bucket.location_code is None), None)

    if unconfirmed is None:
        # 확정할 미확정 재고가 없다. 요청한 위치가 이미 그 위치라면 재전송이고,
        # 다른 위치라면 재배치 요청이다.
        if already_there:
            return LocationResult(LocationOutcome.UNCHANGED)
        return _rejected("location_code", "이미 다른 위치로 확인된 재고입니다")

    if already_there:
        return _rejected(
            "location_code",
            "같은 입고묶음의 합격 재고가 이미 그 위치에 있습니다. 버킷 병합이 필요합니다",
        )

    confirm_bucket_location(session, unconfirmed, command)
    return LocationResult(
        LocationOutcome.APPLIED,
        bucket_id=unconfirmed.bucket_id,
        confirmed_quantity=unconfirmed.quantity,
    )

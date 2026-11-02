"""재고 사용 보류의 조정 계층 (AC-04, AC-10).

보류는 재고 쪽 사실이므로 inventory가 소유하고, 이 모듈은 명령 검증·존재 확인·
판정 어휘만 담당한다. 24-4에서 보류가 조정 계층을 갖는 이유는 "그런 입고묶음이
있는가"(조회)와 "보류를 건다"(쓰기)가 한 트랜잭션에서 함께 판정돼야 하기
때문이다.

조사 절차와 손실 확정은 여기 없다. AC-10의 전체 요구 중 영향 범위 사용 보류만
24-4의 범위이며, 관리자 조사·실제 수량 확정·판매사 통보는 구현하지 않는다.
"""

import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy.orm import Session

from fulfill.modules.inventory.domain import (
    HoldCommand,
    LiftHoldCommand,
    validate_hold,
    validate_lift_hold,
)
from fulfill.modules.inventory.queries import receipt_exists
from fulfill.modules.inventory.repository import insert_hold, lift_hold
from fulfill.shared.validation import ValidationProblem


class HoldOutcome(StrEnum):
    """보류 설정·해제 명령의 판정 결과.

    중복 보류와 중복 해제가 UNCHANGED인 것이 중요하다. 거절이 아니다. 같은 재고를
    두 번 묶으려는 시도는 업무상 사고가 아니라 이미 달성된 상태이며, 거절로 만들면
    담당자는 자기가 무엇을 잘못했는지 찾게 된다.
    """

    # 보류를 새로 걸었거나 활성 보류를 풀었다.
    APPLIED = "APPLIED"
    # 평가했고 바꿀 것이 없었다. 이미 보류 중이거나 활성 보류가 없다.
    UNCHANGED = "UNCHANGED"
    # 명령 자체가 유효하지 않거나 대상 입고묶음이 없다.
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class HoldResult:
    """보류 판정과 그 대상."""

    outcome: HoldOutcome
    # 이번에 걸거나 푼 보류의 id. UNCHANGED·REJECTED에서는 None이다.
    hold_id: uuid.UUID | None = None
    problems: tuple[ValidationProblem, ...] = field(default_factory=tuple)


def _rejected(field_name: str, reason: str) -> HoldResult:
    """사유 하나로 거절한다."""
    return HoldResult(HoldOutcome.REJECTED, problems=(ValidationProblem(field_name, reason),))


def place_inventory_hold(session: Session, command: HoldCommand) -> HoldResult:
    """입고묶음에 사용 보류를 건다 (AC-04, AC-10).

    기존 배정을 찾아 해제하는 코드가 없는 것이 이 함수의 핵심이다. 보류는 신규
    배정 자격을 없애는 사실이며, 이미 붙어 있는 유효 배정을 취소하는 사건이
    아니다. AC-04는 조사 의심 단계에서 유효 배정을 무단 삭제하지 말라고 금지하고,
    NBR-14는 추정만으로 손실을 확정하거나 재배정하지 말라고 요구한다. 배정 조정은
    손실이 확정된 뒤의 일이다(NBR-13).

    보류를 걸어도 배정이 남는다는 것은 재고 쪽에서 보면 "보류와 유효 배정의
    공존"이고, 피킹 쪽에서 보면 "전량 배정됐지만 작업할 수 없는 요청"이다. 후자를
    판정하는 것이 3단계의 관문이다.
    """
    problems = validate_hold(command)
    if problems:
        return HoldResult(HoldOutcome.REJECTED, problems=problems)

    if not receipt_exists(session, command.receipt_id):
        return _rejected("receipt_id", "등록되지 않은 입고묶음입니다")

    hold_id = insert_hold(session, command)
    if hold_id is None:
        return HoldResult(HoldOutcome.UNCHANGED)
    return HoldResult(HoldOutcome.APPLIED, hold_id=hold_id)


def lift_inventory_hold(session: Session, command: LiftHoldCommand) -> HoldResult:
    """입고묶음의 사용 보류를 푼다 (AC-10).

    해제가 배정을 만들지는 않는다. 보류가 풀리면 그 입고묶음의 버킷이 다시 배정
    자격을 얻고, 실제 배정은 다음 배정 실행이 FIFO 순서대로 한다. 해제 시점에
    곧바로 재배정하면 "누가 먼저 기다렸는가"와 무관하게 해제 시각이 순서를
    결정하게 되어 NBR-03의 FIFO를 흔든다.

    대상 입고묶음이 없으면 거절하고, 있지만 활성 보류가 없으면 UNCHANGED다. 둘을
    같은 결과로 묶으면 "입고묶음 id를 잘못 적었다"가 "이미 풀려 있다"로 보고된다.
    """
    problems = validate_lift_hold(command)
    if problems:
        return HoldResult(HoldOutcome.REJECTED, problems=problems)

    if not receipt_exists(session, command.receipt_id):
        return _rejected("receipt_id", "등록되지 않은 입고묶음입니다")

    hold_id = lift_hold(session, command)
    if hold_id is None:
        return HoldResult(HoldOutcome.UNCHANGED)
    return HoldResult(HoldOutcome.APPLIED, hold_id=hold_id)

"""입고·검수·귀속 확인의 조정 계층 (TX-04).

세 use case를 한 파일에 둔다. TX-04가 "Receipt·QCDecision·StockPosition·Hold
영향 범위"를 함께 판정할 하나의 범위로 묶었고, 셋이 결과 타입을 공유한다.

이 모듈은 어느 모듈의 ORM도 직접 보지 않는다. inventory가 공개한 입구
(domain·repository·queries)와 seller_contracts의 조회만 쓴다.
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from fulfill.modules.inventory.domain import (
    REVIEW_CONFIRMED,
    AdviceCommand,
    OwnershipReviewCommand,
    QcCommand,
    ReceiptCommand,
    validate_advice,
    validate_ownership_review,
    validate_qc_decision,
    validate_receipt,
)
from fulfill.modules.inventory.queries import (
    find_sku_ids_by_codes,
    summarize_advice,
)
from fulfill.modules.inventory.repository import (
    NewOwnershipReview,
    NewReceipt,
    apply_qc_decision,
    insert_advice,
    insert_receipt,
    lock_ownership_status,
    lock_pending_quantity,
    record_ownership_review,
)
from fulfill.modules.seller_contracts.queries import find_seller_id_by_code
from fulfill.shared.validation import ValidationProblem, normalize_text


class ReceivingOutcome(StrEnum):
    """입고·검수 명령의 판정 결과.

    APPLIED와 REJECTED 둘뿐이다. 24-1의 UNCHANGED·CONFLICT는 업무 멱등키가
    있을 때만 의미가 있는데, 입고와 검수에는 그런 키가 없다. 같은 물건이 두 번
    도착하는 것은 재전달이 아니라 분할 입고다.

    전송 재시도 멱등(operationId)은 20단계가 업무 중복과 분리해 둔 별개 문제이며
    아직 도입하지 않았다.
    """

    APPLIED = "APPLIED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class ReceivingResult:
    """판정 결과와 그 근거."""

    outcome: ReceivingOutcome
    # 명령에 따라 둘 중 하나만 채워진다. 예정 통보는 advice_id, 실도착·검수·귀속
    # 확인은 receipt_id다.
    receipt_id: uuid.UUID | None = None
    advice_id: uuid.UUID | None = None
    problems: tuple[ValidationProblem, ...] = ()


def _rejected(field: str, reason: str) -> ReceivingResult:
    """사유 하나로 거절한다."""
    return ReceivingResult(ReceivingOutcome.REJECTED, problems=(ValidationProblem(field, reason),))


def _resolve_attribution(
    session: Session, seller_code: str | None, sku_code: str | None
) -> tuple[uuid.UUID, uuid.UUID] | ReceivingResult | None:
    """판매사·품목 코드를 id로 해석한다.

    세 가지를 구분해 돌려준다. 해석 성공은 id 쌍, 해석 실패는 거절, 코드가 아예
    없으면 None이다.

    마지막 경우가 AC-08의 핵심이다. 미확인 입고는 "조회에 실패한 것"이 아니라
    "조회할 것이 없는 것"이다. 실패로 다루면 거절되어 미확인품을 기록할 수 없다.
    """
    seller_code = normalize_text(seller_code or "")
    sku_code = normalize_text(sku_code or "")
    if not seller_code and not sku_code:
        return None

    seller_id = find_seller_id_by_code(session, seller_code)
    if seller_id is None:
        return _rejected("seller_code", "등록되지 않은 판매사입니다")

    sku_id = find_sku_ids_by_codes(session, seller_id, [sku_code]).get(sku_code)
    if sku_id is None:
        return _rejected("sku_code", "이 판매사에 등록되지 않은 SKU입니다")

    return seller_id, sku_id


def record_inbound_advice(session: Session, command: AdviceCommand) -> ReceivingResult:
    """입고 예정 통보를 접수한다 (AC-06).

    예정량은 약속이지 재고가 아니다. 이 명령은 재고 버킷을 만들지 않으며, 그것이
    AC-06의 "예정량 600개를 물리 정상 재고로 취급하지 않는다"를 보장한다.
    """
    problems = validate_advice(command)
    if problems:
        return ReceivingResult(ReceivingOutcome.REJECTED, problems=problems)

    resolved = _resolve_attribution(session, command.seller_code, command.sku_code)
    if isinstance(resolved, ReceivingResult):
        return resolved
    if resolved is None:
        # 도메인이 두 코드를 요구하므로 도달할 수 없다.
        return _rejected("seller_code", "판매사 코드가 비어 있습니다")

    _, sku_id = resolved
    advice_id = insert_advice(session, command, sku_id)
    return ReceivingResult(ReceivingOutcome.APPLIED, advice_id=advice_id)


def record_receipt(session: Session, command: ReceiptCommand) -> ReceivingResult:
    """실도착을 접수한다 (AC-06, AC-08)."""
    problems = validate_receipt(command)
    if problems:
        return ReceivingResult(ReceivingOutcome.REJECTED, problems=problems)

    # FK에 맡기면 담당자가 업무 사유 대신 서버 오류를 받는다.
    if command.advice_id is not None and summarize_advice(session, command.advice_id) is None:
        return _rejected("advice_id", "등록되지 않은 입고 예정입니다")

    resolved = _resolve_attribution(session, command.seller_code, command.sku_code)
    if isinstance(resolved, ReceivingResult):
        return resolved
    seller_id, sku_id = resolved if resolved is not None else (None, None)

    receipt_id = insert_receipt(
        session, NewReceipt(command=command, seller_id=seller_id, sku_id=sku_id)
    )
    return ReceivingResult(ReceivingOutcome.APPLIED, receipt_id=receipt_id)


def record_qc_decision(session: Session, command: QcCommand) -> ReceivingResult:
    """검수 판정을 접수한다 (AC-07).

    순서가 전부다. 잠그고 → 검증하고 → 적용한다. 잠금을 검증 뒤로 옮기면 과검수
    거절이 무용지물이 된다. 읽은 값과 쓸 때의 값이 달라질 수 있기 때문이며,
    24-1에서 "조회가 비어 있다고 답한 뒤 다른 트랜잭션이 끼어든다"와 같은 문제다.

    입고 존재를 먼저 확인한다. 없는 입고에 대해 lock_pending_quantity는 0을
    돌려주고 도메인은 "미검수 수량 0개를 넘습니다"로 거절하는데, 그것은 틀린
    사유다. 입고가 없는 것과 미검수가 0인 것은 다른 사실이다.
    """
    if lock_ownership_status(session, command.receipt_id) is None:
        return _rejected("receipt_id", "등록되지 않은 입고입니다")

    pending_quantity = lock_pending_quantity(session, command.receipt_id)
    problems = validate_qc_decision(command, pending_quantity)
    if problems:
        return ReceivingResult(ReceivingOutcome.REJECTED, problems=problems)

    apply_qc_decision(session, command)
    return ReceivingResult(ReceivingOutcome.APPLIED, receipt_id=command.receipt_id)


def review_ownership(session: Session, command: OwnershipReviewCommand) -> ReceivingResult:
    """귀속 확인 시도를 접수한다 (AC-08).

    confirm이 아니라 review인 이유가 있다. 이 함수의 결과 중 둘은 확인하지
    못했다는 기록이다. confirm이라고 부르면 실패 경로가 이름과 어긋나고, AC-08이
    요구하는 "확인 시도" 기록이 부차적인 것처럼 읽힌다.

    코드 해석은 확인에 성공했을 때만 한다. 확인하지 못한 검토에 소유자 코드가
    오는 것은 도메인이 이미 거절하므로, 여기서 해석하면 도달할 수 없는 경로가
    생긴다.
    """
    current_status = lock_ownership_status(session, command.receipt_id)
    if current_status is None:
        return _rejected("receipt_id", "등록되지 않은 입고입니다")

    problems = validate_ownership_review(command, current_status)
    if problems:
        return ReceivingResult(ReceivingOutcome.REJECTED, problems=problems)

    seller_id: uuid.UUID | None = None
    sku_id: uuid.UUID | None = None
    if command.outcome == REVIEW_CONFIRMED:
        resolved = _resolve_attribution(session, command.seller_code, command.sku_code)
        if isinstance(resolved, ReceivingResult):
            return resolved
        if resolved is None:
            # 도메인이 CONFIRMED에 코드를 요구하므로 도달할 수 없다.
            return _rejected("seller_code", "판매사 코드가 비어 있습니다")
        seller_id, sku_id = resolved

    record_ownership_review(
        session,
        NewOwnershipReview(command=command, seller_id=seller_id, sku_id=sku_id),
    )
    return ReceivingResult(ReceivingOutcome.APPLIED, receipt_id=command.receipt_id)

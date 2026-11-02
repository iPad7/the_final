"""입고·검수의 순수 도메인 로직 (BC-03).

이 모듈은 FastAPI도 SQLAlchemy도 import하지 않는다. 웹 프레임워크나 DB 없이
검증할 수 있어야 하며(ADR23 §3), import 계약이 이를 강제한다.

아래 업무 어휘를 `models.py`가 가져다 쓴다. 의존 방향이 models → domain인 것이
중요하다. 반대로 domain이 models를 보면 SQLAlchemy가 딸려 와 순수성이 깨진다.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from fulfill.shared.validation import ValidationProblem, normalize_text

# 실물의 판매사·품목 귀속이 확인됐는지. 실도착과는 다른 사실이다.
OWNERSHIP_CONFIRMED = "CONFIRMED"
OWNERSHIP_UNKNOWN = "UNKNOWN"

# 귀속 확인 시도 하나의 결과. 귀속 상태(위 2개)와 집합이 다르다. 한 이름으로
# 묶으면 "확인하지 못했다"와 "관리자에게 올렸다"를 담을 데가 없어진다.
REVIEW_CONFIRMED = "CONFIRMED"
REVIEW_STILL_UNKNOWN = "STILL_UNKNOWN"
REVIEW_ESCALATED = "ESCALATED"

REVIEW_OUTCOMES = (REVIEW_CONFIRMED, REVIEW_STILL_UNKNOWN, REVIEW_ESCALATED)

# 검수자가 내리는 판정. 미검수를 뜻하는 값은 없다 — 판정의 부재는 판정이 아니다.
QC_PASS = "PASS"
QC_FAIL = "FAIL"

# 재고 버킷이 놓인 검수 상태. 위 판정과 구분한다. 판정은 일어난 사건이고
# 이것은 그 사건을 적용한 뒤 실물이 머무는 상태다.
QC_STATE_PENDING = "PENDING"
QC_STATE_PASSED = "PASSED"
QC_STATE_FAILED = "FAILED"

# 판정이 수량을 어느 버킷으로 옮기는지. 이 표가 있어야 "검수는 버킷 사이 수량
# 이동"이라는 결정이 코드에 한 곳으로 모인다.
BUCKET_STATE_BY_QC_RESULT = {
    QC_PASS: QC_STATE_PASSED,
    QC_FAIL: QC_STATE_FAILED,
}

# 재고 사용 보류를 걸게 되는 사유. AC-10이 "조사 근거"를 업무 근거로 요구하므로
# 자유 문자열로 두지 않는다. 다만 사유별 기한·승인 요건은 미확정 운영
# 파라미터(R-05)이므로 코드에 박지 않는다.
#
# 수량 차이 의심. NBR-14가 "추정만으로 손실 확정·재배정 금지"라고 한 그 단계다.
HOLD_REASON_COUNT_DISCREPANCY = "COUNT_DISCREPANCY"
# 사후 품질 의심. QC 불합격과 다르다 — 불합격은 판정이고 이쪽은 조사 착수다.
HOLD_REASON_QUALITY_SUSPECTED = "QUALITY_SUSPECTED"
# 위 둘로 분류되지 않는 관리자 판단. note에 근거를 남기게 한다.
HOLD_REASON_OTHER = "OTHER"

HOLD_REASONS = (
    HOLD_REASON_COUNT_DISCREPANCY,
    HOLD_REASON_QUALITY_SUSPECTED,
    HOLD_REASON_OTHER,
)

# 길이 상한. DB 컬럼 길이와 같은 값이 두 곳에 존재하지만, 도메인은 SQLAlchemy를
# import할 수 없으므로 피할 수 없다. 두 값이 어긋나면 깨지는 테스트를 둔다.
MAX_CONFIRMED_BY_LENGTH = 100
MAX_DECIDED_BY_LENGTH = 100
MAX_HELD_BY_LENGTH = 100
MAX_LIFTED_BY_LENGTH = 100
MAX_LOCATION_CODE_LENGTH = 100
MAX_REVIEWED_BY_LENGTH = 100
# 사유 컬럼은 Text라 DB 제한이 없다. 과도한 입력을 막는 도메인 정책이며 대응하는
# 컬럼 길이가 없다.
MAX_HOLD_NOTE_LENGTH = 1000
MAX_REVIEW_NOTE_LENGTH = 1000


@dataclass(frozen=True)
class AdviceCommand:
    """판매사가 입고 예정을 통보하는 명령."""

    seller_code: str
    sku_code: str
    expected_quantity: int
    advised_at: datetime


def validate_advice(command: AdviceCommand) -> tuple[ValidationProblem, ...]:
    """입고 예정 통보의 유효성을 판정한다 (AC-06).

    판매사와 품목이 필수인 점이 실도착과 다르다. 예정 통보는 판매사가 보내는
    것이라 보낸 쪽이 누구인지 늘 알며, inbound_advice.sku_id도 NOT NULL이다.
    실도착은 소유자를 모르는 물건도 기록해야 해서 선택이었다(AC-08).
    """
    problems: list[ValidationProblem] = []

    if command.expected_quantity <= 0:
        problems.append(ValidationProblem("expected_quantity", "수량은 양수여야 합니다"))

    if not normalize_text(command.seller_code):
        problems.append(ValidationProblem("seller_code", "판매사 코드가 비어 있습니다"))
    if not normalize_text(command.sku_code):
        problems.append(ValidationProblem("sku_code", "SKU 코드가 비어 있습니다"))

    if command.advised_at.tzinfo is None:
        problems.append(ValidationProblem("advised_at", "시간대가 없는 통보 시각입니다"))

    return tuple(problems)


@dataclass(frozen=True)
class ReceiptCommand:
    """실도착 한 건을 기록하라는 명령.

    귀속이 선택인 것이 AC-08의 출발점이다. 소유자를 모르는 물건도 도착한 것은
    사실이므로 기록할 수 있어야 한다.
    """

    arrived_quantity: int
    confirmed_by: str
    arrived_at: datetime
    seller_code: str | None = None
    sku_code: str | None = None
    advice_id: uuid.UUID | None = None


def validate_receipt(command: ReceiptCommand) -> tuple[ValidationProblem, ...]:
    """실도착 기록의 유효성을 판정한다 (AC-06).

    예정량과 비교하지 않는다. "예정 600에 590이 왔다"는 오류가 아니라 AC-06이
    정상 경로로 처리하라고 요구하는 바로 그 시나리오다. 많이 온 경우도 실물이
    왔다는 사실은 기록해야 하므로 거절하지 않는다. 차이는 조회가 계산해 보여줄
    값이지 접수를 막을 근거가 아니다.

    판매사와 품목은 둘 다 있거나 둘 다 없어야 한다. SKU가 판매사에 종속되므로
    품목만 아는 상태는 성립하지 않고, DB도 같은 규칙을 제약으로 막는다.

    위치를 받지 않는다. 입고 시점에 피킹 위치는 아직 확정되지 않았고, AC-09가
    "임시 구역 입고 자체를 피킹 위치 확인 완료로 간주하지 않는다"고 요구한다.
    위치 확인은 별도 작업이므로 버킷은 위치 NULL로 만들어진다.
    """
    problems: list[ValidationProblem] = []

    if command.arrived_quantity <= 0:
        problems.append(ValidationProblem("arrived_quantity", "수량은 양수여야 합니다"))

    seller_code = normalize_text(command.seller_code or "")
    sku_code = normalize_text(command.sku_code or "")
    if bool(seller_code) != bool(sku_code):
        problems.append(
            ValidationProblem("sku_code", "판매사와 품목은 함께 지정하거나 함께 비워야 합니다")
        )

    confirmed_by = normalize_text(command.confirmed_by)
    if not confirmed_by:
        problems.append(ValidationProblem("confirmed_by", "입고 담당자가 비어 있습니다"))
    elif len(confirmed_by) > MAX_CONFIRMED_BY_LENGTH:
        problems.append(
            ValidationProblem("confirmed_by", f"{MAX_CONFIRMED_BY_LENGTH}자를 넘습니다")
        )

    if command.arrived_at.tzinfo is None:
        problems.append(ValidationProblem("arrived_at", "시간대가 없는 도착 시각입니다"))

    return tuple(problems)


@dataclass(frozen=True)
class QcCommand:
    """검수 판정 하나를 기록하라는 명령."""

    receipt_id: uuid.UUID
    result: str
    quantity: int
    decided_by: str
    occurred_at: datetime
    # 불합격품을 둘 격리 구역. AC-07이 격리 구역을 업무 근거로 요구한다.
    location_code: str | None = None


def validate_qc_decision(
    command: QcCommand, pending_quantity: int
) -> tuple[ValidationProblem, ...]:
    """검수 판정의 유효성을 판정한다 (AC-07).

    `pending_quantity`는 이 입고의 현재 미검수 잔량이며 인자로 받는다. 도메인이
    직접 조회하면 SQLAlchemy가 필요해져 순수성이 깨진다. 조회는 조정 계층의
    일이고 여기서는 "이 숫자들이 주어졌을 때 유효한가"만 답한다.

    과검수를 거절하는 것이 핵심이다. stock_position의 `quantity >= 0`이 DB에서도
    막지만, 거기까지 가면 담당자는 업무 사유 대신 서버 오류를 받는다.

    예외를 던지지 않고 사유 목록을 반환한다. 거절은 예상된 업무 결과다.
    """
    problems: list[ValidationProblem] = []

    if command.result not in BUCKET_STATE_BY_QC_RESULT:
        problems.append(ValidationProblem("result", "합격 또는 불합격이어야 합니다"))

    if command.quantity <= 0:
        problems.append(ValidationProblem("quantity", "수량은 양수여야 합니다"))
    elif command.quantity > pending_quantity:
        problems.append(
            ValidationProblem(
                "quantity",
                f"미검수 수량 {pending_quantity}개를 넘습니다",
            )
        )

    decided_by = normalize_text(command.decided_by)
    if not decided_by:
        problems.append(ValidationProblem("decided_by", "검수 담당자가 비어 있습니다"))
    elif len(decided_by) > MAX_DECIDED_BY_LENGTH:
        problems.append(ValidationProblem("decided_by", f"{MAX_DECIDED_BY_LENGTH}자를 넘습니다"))

    # 위치 코드는 버킷 유니크 키에 들어간다. 정규화하지 않으면 "Q-01"과 "Q-01 "이
    # 서로 다른 버킷이 되어 수량 보존이 깨진다.
    location_code = normalize_text(command.location_code or "")
    if len(location_code) > MAX_LOCATION_CODE_LENGTH:
        problems.append(
            ValidationProblem("location_code", f"{MAX_LOCATION_CODE_LENGTH}자를 넘습니다")
        )

    # 시간대가 없는 datetime은 시점이 모호하다. 판정 시각은 AC-07의 업무 근거이고
    # 여기서 생긴 모호함은 나중에 복구할 수 없다.
    if command.occurred_at.tzinfo is None:
        problems.append(ValidationProblem("occurred_at", "시간대가 없는 판정 시각입니다"))

    return tuple(problems)


@dataclass(frozen=True)
class OwnershipReviewCommand:
    """귀속 확인 시도 하나를 기록하라는 명령."""

    receipt_id: uuid.UUID
    outcome: str
    reviewed_by: str
    occurred_at: datetime
    # CONFIRMED일 때만 의미가 있다. 어느 판매사의 어느 품목인지 지목한다.
    seller_code: str | None = None
    sku_code: str | None = None
    # 확인하지 못한 이유, 관리자 판단 근거.
    note: str | None = None
    # 이 시도에 적용한 운영사 공통 확인 기한.
    due_at: datetime | None = None


def validate_ownership_review(
    command: OwnershipReviewCommand, current_status: str
) -> tuple[ValidationProblem, ...]:
    """귀속 확인 시도의 유효성을 판정한다 (AC-08).

    `current_status`는 대상 입고의 현재 귀속 상태이며 인자로 받는다. 조회는
    조정 계층의 일이다.

    판매사·품목 코드는 길이를 검사하지 않는다. 저장하는 값이 아니라 조회 키이고,
    등록되지 않은 코드는 조정 계층이 "등록되지 않았다"로 답한다. 길이 상한은
    저장되는 값에만 필요하다.
    """
    problems: list[ValidationProblem] = []

    if command.outcome not in REVIEW_OUTCOMES:
        problems.append(ValidationProblem("outcome", "허용되지 않는 검토 결과입니다"))

    # 이미 확인된 귀속을 되돌리는 것은 기록 정정(AC-18)이고 별도 권한이다. 조용히
    # 허용하면 이력 보존을 우회하는 경로가 생긴다.
    if current_status != OWNERSHIP_UNKNOWN:
        problems.append(ValidationProblem("receipt_id", "이미 귀속이 확인된 입고입니다"))

    seller_code = normalize_text(command.seller_code or "")
    sku_code = normalize_text(command.sku_code or "")

    if command.outcome == REVIEW_CONFIRMED:
        # 둘 중 하나만 있으면 귀속을 지목한 것이 아니다. SKU는 판매사에 종속되므로
        # 품목만 알고 판매사를 모르는 상태는 성립하지 않는다.
        if not seller_code:
            problems.append(ValidationProblem("seller_code", "판매사 코드가 비어 있습니다"))
        if not sku_code:
            problems.append(ValidationProblem("sku_code", "SKU 코드가 비어 있습니다"))
    else:
        # 확인하지 못했다면서 소유자를 적는 입력은 모순이다. 받아 주면 소유자를
        # 모르는 상태에서 특정 판매사 기준을 적용하는 길이 열린다(AC-08 위반 조항).
        if seller_code or sku_code:
            problems.append(
                ValidationProblem("outcome", "확인하지 못한 검토에는 소유자를 적을 수 없습니다")
            )
        # 사유 없는 "확인 못 함"은 기록이 아니라 빈칸이다. AC-08은 관리자 판단을
        # 근거로 요구한다.
        if not normalize_text(command.note or ""):
            problems.append(ValidationProblem("note", "확인하지 못한 사유가 필요합니다"))

    reviewed_by = normalize_text(command.reviewed_by)
    if not reviewed_by:
        problems.append(ValidationProblem("reviewed_by", "검토 담당자가 비어 있습니다"))
    elif len(reviewed_by) > MAX_REVIEWED_BY_LENGTH:
        problems.append(ValidationProblem("reviewed_by", f"{MAX_REVIEWED_BY_LENGTH}자를 넘습니다"))

    note = normalize_text(command.note or "")
    if len(note) > MAX_REVIEW_NOTE_LENGTH:
        problems.append(ValidationProblem("note", f"{MAX_REVIEW_NOTE_LENGTH}자를 넘습니다"))

    if command.occurred_at.tzinfo is None:
        problems.append(ValidationProblem("occurred_at", "시간대가 없는 검토 시각입니다"))
    if command.due_at is not None and command.due_at.tzinfo is None:
        problems.append(ValidationProblem("due_at", "시간대가 없는 확인 기한입니다"))

    return tuple(problems)


@dataclass(frozen=True)
class HoldCommand:
    """입고묶음 하나에 사용 보류를 걸라는 명령 (AC-04, AC-10)."""

    receipt_id: uuid.UUID
    reason: str
    held_by: str
    occurred_at: datetime
    # 조사 근거. OTHER 사유에서는 필수다.
    note: str | None = None


def validate_hold(command: HoldCommand) -> tuple[ValidationProblem, ...]:
    """보류 설정 명령의 유효성을 판정한다 (AC-10).

    대상 입고묶음이 존재하는지, 이미 보류 중인지는 보지 않는다. 전자는 조회이고
    후자는 DB의 부분 유니크 인덱스가 판정하는 사실이므로 둘 다 조정 계층의
    일이다.
    """
    problems: list[ValidationProblem] = []

    if command.reason not in HOLD_REASONS:
        problems.append(ValidationProblem("reason", "허용되지 않는 보류 사유입니다"))

    held_by = normalize_text(command.held_by)
    if not held_by:
        problems.append(ValidationProblem("held_by", "보류 주체가 비어 있습니다"))
    elif len(held_by) > MAX_HELD_BY_LENGTH:
        problems.append(ValidationProblem("held_by", f"{MAX_HELD_BY_LENGTH}자를 넘습니다"))

    note = normalize_text(command.note or "")
    if len(note) > MAX_HOLD_NOTE_LENGTH:
        problems.append(ValidationProblem("note", f"{MAX_HOLD_NOTE_LENGTH}자를 넘습니다"))
    # 분류되지 않는 사유를 근거 없이 받으면 "관리자가 판단했다"는 사실만 남고
    # 무엇을 근거로 재고를 묶었는지가 사라진다. AC-10은 확인 자료를 요구한다.
    if command.reason == HOLD_REASON_OTHER and not note:
        problems.append(ValidationProblem("note", "기타 사유에는 조사 근거가 필요합니다"))

    # 실제 발생 시각을 기록 시각과 구분하려면 어느 시점을 말하는지가 확정돼야
    # 한다. 시간대 없는 값은 그 자체로 모호하다.
    if command.occurred_at.tzinfo is None:
        problems.append(ValidationProblem("occurred_at", "시간대가 없는 보류 시각입니다"))

    return tuple(problems)


@dataclass(frozen=True)
class LiftHoldCommand:
    """입고묶음의 사용 보류를 풀라는 명령 (AC-10)."""

    receipt_id: uuid.UUID
    lifted_by: str
    occurred_at: datetime
    # 무엇을 확인해 풀었는지. 설정과 달리 항상 요구한다.
    note: str


def validate_lift_hold(command: LiftHoldCommand) -> tuple[ValidationProblem, ...]:
    """보류 해제 명령의 유효성을 판정한다 (AC-10).

    해제에는 사유 어휘가 없다. 설정은 "무엇을 의심하는가"에 따라 갈라지지만 해제는
    "조사가 끝났다" 한 종류이며, 갈라지는 것은 그때 확인한 내용이다. 그래서 어휘
    대신 근거를 필수로 받는다.

    활성 보류가 실제로 있는지는 보지 않는다. 조회이므로 조정 계층의 일이다.
    """
    problems: list[ValidationProblem] = []

    lifted_by = normalize_text(command.lifted_by)
    if not lifted_by:
        problems.append(ValidationProblem("lifted_by", "해제 주체가 비어 있습니다"))
    elif len(lifted_by) > MAX_LIFTED_BY_LENGTH:
        problems.append(ValidationProblem("lifted_by", f"{MAX_LIFTED_BY_LENGTH}자를 넘습니다"))

    note = normalize_text(command.note)
    # 근거 없는 해제를 받으면 조사 없이 재고를 되살리는 경로가 생긴다. NBR-14는
    # 추정만으로 재배정하지 말라고 요구하며, 해제는 재배정을 다시 여는 행위다.
    if not note:
        problems.append(ValidationProblem("note", "해제 근거가 필요합니다"))
    elif len(note) > MAX_HOLD_NOTE_LENGTH:
        problems.append(ValidationProblem("note", f"{MAX_HOLD_NOTE_LENGTH}자를 넘습니다"))

    if command.occurred_at.tzinfo is None:
        problems.append(ValidationProblem("occurred_at", "시간대가 없는 해제 시각입니다"))

    return tuple(problems)


@dataclass(frozen=True)
class ConfirmLocationCommand:
    """합격 재고의 실제 피킹 위치를 확인했다는 명령 (AC-09)."""

    receipt_id: uuid.UUID
    # 확인된 실제 피킹 위치. 비울 수 없다.
    location_code: str
    confirmed_by: str
    occurred_at: datetime


def validate_confirm_location(command: ConfirmLocationCommand) -> tuple[ValidationProblem, ...]:
    """위치 확인 명령의 유효성을 판정한다 (AC-09).

    위치 코드를 비울 수 없다. location_code의 NULL이 곧 "피킹 위치 미확정"이므로,
    빈 값으로 확인하는 것은 "확인했는데 미확정"이라는 모순이고 AC-09가 금지한
    "임시 구역 입고를 위치 확인 완료로 간주"와 같은 결과가 된다.

    확인할 합격 재고가 있는지, 이미 다른 위치로 확인됐는지는 보지 않는다. 둘 다
    조회이므로 조정 계층의 일이다.
    """
    problems: list[ValidationProblem] = []

    location_code = normalize_text(command.location_code)
    if not location_code:
        problems.append(ValidationProblem("location_code", "피킹 위치가 비어 있습니다"))
    elif len(location_code) > MAX_LOCATION_CODE_LENGTH:
        problems.append(
            ValidationProblem("location_code", f"{MAX_LOCATION_CODE_LENGTH}자를 넘습니다")
        )

    confirmed_by = normalize_text(command.confirmed_by)
    if not confirmed_by:
        problems.append(ValidationProblem("confirmed_by", "확인 주체가 비어 있습니다"))
    elif len(confirmed_by) > MAX_CONFIRMED_BY_LENGTH:
        problems.append(
            ValidationProblem("confirmed_by", f"{MAX_CONFIRMED_BY_LENGTH}자를 넘습니다")
        )

    # AC-09는 "실제 피킹 위치 확인 사실"을 업무 근거로 요구한다. 언제 확인했는지가
    # 모호하면 그 사실이 기록으로 성립하지 않는다.
    if command.occurred_at.tzinfo is None:
        problems.append(ValidationProblem("occurred_at", "시간대가 없는 확인 시각입니다"))

    return tuple(problems)

"""출고 요청 접수의 순수 도메인 로직 (BC-02).

이 모듈은 FastAPI도 SQLAlchemy도 import하지 않는다. 웹 프레임워크나 DB 없이
검증할 수 있어야 하며(ADR23 §3), import 계약이 이를 강제한다.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

# 아래 정규화 규칙이 바뀔 때마다 올린다. fingerprint와 함께 저장해, 서로 다른
# 규칙으로 만든 값을 같은 의미인 것처럼 비교하지 않도록 한다.
NORMALIZATION_VERSION = 1

# 길이 상한. DB 컬럼 길이와 같은 값이 두 곳에 존재하지만, 도메인은 SQLAlchemy를
# import할 수 없으므로 피할 수 없다. 두 값이 어긋나면 깨지는 테스트를 둔다.
MAX_SELLER_CODE_LENGTH = 50
MAX_EXTERNAL_REQUEST_ID_LENGTH = 100
MAX_SKU_CODE_LENGTH = 100
MAX_RECIPIENT_NAME_LENGTH = 100
MAX_RECIPIENT_PHONE_LENGTH = 40
# 주소 컬럼은 Text라 DB 제한이 없다. 이 값은 과도한 입력을 막는 도메인 정책이며
# 대응하는 컬럼 길이가 없다.
MAX_RECIPIENT_ADDRESS_LENGTH = 1000

_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    """앞뒤 공백을 제거하고 연속 공백을 하나로 합친다.

    "A  101"과 "A 101"은 같은 주소이므로 합치는 것이 맞다. 다만 공백을 전부
    제거하면 서로 다른 주소가 합쳐질 수 있어 거기까지는 가지 않는다.
    """
    return _WHITESPACE_RUN.sub(" ", value).strip()


@dataclass(frozen=True)
class RequestedLine:
    sku_code: str
    quantity: int


@dataclass(frozen=True)
class Recipient:
    name: str
    address: str
    phone: str


@dataclass(frozen=True)
class IntakeCommand:
    """판매사 OMS로부터 수신한 출고 지시 한 건."""

    seller_code: str
    external_request_id: str
    lines: tuple[RequestedLine, ...]
    recipient: Recipient
    initial_due_at: datetime
    desired_ship_date: date | None
    occurred_at: datetime


@dataclass(frozen=True)
class ValidationProblem:
    field: str
    reason: str


def _check_length(problems: list[ValidationProblem], field: str, value: str, limit: int) -> None:
    """길이 상한 검사.

    상한이 없으면 과도한 입력이 검증을 통과한 뒤 INSERT에서 DB 오류가 되고,
    판매사는 사유를 읽을 수 있는 거절 대신 서버 오류를 받는다.
    """
    if len(value) > limit:
        problems.append(ValidationProblem(field, f"{limit}자를 넘습니다"))


def validate(command: IntakeCommand) -> tuple[ValidationProblem, ...]:
    """요청 자체의 유효성을 판정한다 (AC-01).

    예외를 던지지 않고 사유 목록을 반환한다. 거절은 판매사가 읽을 수 있어야 하는
    예상된 업무 결과이지 예외 상황이 아니며, 예외로 던지면 첫 문제에서 멈춰
    나머지를 알려줄 수 없다.

    재고는 일부러 조회하지 않는다. 재고가 없는 요청도 유효하며 대기한다
    (NBR-01). 무효가 될 수 있는 것은 요청 자체뿐이다.

    길이는 정규화된 값을 기준으로 본다. 저장도 정규화된 값으로 하므로 저장값과
    비교값이 어긋나지 않는다.
    """
    problems: list[ValidationProblem] = []

    seller_code = normalize_text(command.seller_code)
    if not seller_code:
        problems.append(ValidationProblem("seller_code", "판매사 코드가 비어 있습니다"))
    _check_length(problems, "seller_code", seller_code, MAX_SELLER_CODE_LENGTH)

    external_request_id = normalize_text(command.external_request_id)
    if not external_request_id:
        problems.append(ValidationProblem("external_request_id", "외부 요청 ID가 비어 있습니다"))
    _check_length(
        problems,
        "external_request_id",
        external_request_id,
        MAX_EXTERNAL_REQUEST_ID_LENGTH,
    )

    if not command.lines:
        problems.append(ValidationProblem("lines", "출고 품목이 없습니다"))

    seen_codes: set[str] = set()
    for index, line in enumerate(command.lines):
        code = normalize_text(line.sku_code)
        if not code:
            problems.append(
                ValidationProblem(f"lines[{index}].sku_code", "SKU 코드가 비어 있습니다")
            )
        elif code in seen_codes:
            # 한 요청에 같은 SKU가 두 번 있으면 FIFO 순번이 어느 라인에 속하는지
            # 모호해진다. NBR-06은 구성 변경을 취소 후 신규 요청으로 처리하므로
            # 중복 라인이 필요한 경우는 없다.
            problems.append(ValidationProblem(f"lines[{index}].sku_code", "같은 SKU가 중복됩니다"))
        else:
            seen_codes.add(code)
        _check_length(problems, f"lines[{index}].sku_code", code, MAX_SKU_CODE_LENGTH)

        if line.quantity <= 0:
            problems.append(ValidationProblem(f"lines[{index}].quantity", "수량은 양수여야 합니다"))

    name = normalize_text(command.recipient.name)
    if not name:
        problems.append(ValidationProblem("recipient.name", "수취인 이름이 비어 있습니다"))
    _check_length(problems, "recipient.name", name, MAX_RECIPIENT_NAME_LENGTH)

    address = normalize_text(command.recipient.address)
    if not address:
        problems.append(ValidationProblem("recipient.address", "주소가 비어 있습니다"))
    _check_length(problems, "recipient.address", address, MAX_RECIPIENT_ADDRESS_LENGTH)

    phone = normalize_text(command.recipient.phone)
    if not phone:
        problems.append(ValidationProblem("recipient.phone", "연락처가 비어 있습니다"))
    _check_length(problems, "recipient.phone", phone, MAX_RECIPIENT_PHONE_LENGTH)

    # 시간대가 없는 datetime은 시점이 모호하다. 기한은 납기 위반 판정의 근거이고,
    # 여기서 생긴 모호함은 나중에 복구할 수 없다.
    if command.initial_due_at.tzinfo is None:
        problems.append(ValidationProblem("initial_due_at", "시간대가 없는 기한입니다"))
    if command.occurred_at.tzinfo is None:
        problems.append(ValidationProblem("occurred_at", "시간대가 없는 발생 시각입니다"))

    return tuple(problems)


def fingerprint(command: IntakeCommand) -> str:
    """두 요청이 같은 내용인지 판정할 기준이 되는 해시.

    무엇을 넣고 왜 중요한지 (AC-02):

    - 라인은 정렬한다. 순서만 바꾼 재전달도 같은 요청이다.
    - 수취인을 포함한다. 주소를 빼면 주소만 바뀐 요청이 "같은 요청"으로
      판정되어 물건이 엉뚱한 곳으로 간다.
    - 기한은 UTC로 정규화한다. 같은 시점을 다른 offset으로 쓴 것이 변경으로
      보이지 않게 한다.
    - occurred_at은 제외한다. 재전송이 새로운 발행 시각을 달고 올 수 있는데,
      포함하면 모든 재전송이 CONFLICT가 된다.
    - effective_due_at도 제외한다. 기한 재협의는 별도 권한의 리비전이지 요청
      내용의 변경이 아니다.
    - 규칙 버전을 해시에 넣는다. 규칙 변경 전후의 값이 우연히 같아질 수 없다.
    """
    lines = sorted(
        ((normalize_text(line.sku_code), line.quantity) for line in command.lines),
        key=lambda pair: pair[0],
    )
    payload: dict[str, object] = {
        "normalization_version": NORMALIZATION_VERSION,
        "seller_code": normalize_text(command.seller_code),
        "external_request_id": normalize_text(command.external_request_id),
        "lines": [[code, quantity] for code, quantity in lines],
        "recipient": [
            normalize_text(command.recipient.name),
            normalize_text(command.recipient.address),
            normalize_text(command.recipient.phone),
        ],
        "initial_due_at": command.initial_due_at.astimezone(UTC).isoformat(),
        "desired_ship_date": (
            command.desired_ship_date.isoformat() if command.desired_ship_date else None
        ),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

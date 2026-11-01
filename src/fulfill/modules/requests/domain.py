"""출고 요청 접수의 순수 도메인 로직 (BC-02).

이 모듈은 FastAPI도 SQLAlchemy도 import하지 않는다. 웹 프레임워크나 DB 없이
검증할 수 있어야 하며(ADR23 §3), import 계약이 이를 강제한다.
"""

import hashlib
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from fulfill.shared.validation import ValidationProblem, normalize_text

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
MAX_REVISED_BY_LENGTH = 100
# 주소·사유 컬럼은 Text라 DB 제한이 없다. 아래 두 값은 과도한 입력을 막는 도메인
# 정책이며 대응하는 컬럼 길이가 없다.
MAX_RECIPIENT_ADDRESS_LENGTH = 1000
MAX_REVISION_REASON_LENGTH = 1000


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


@dataclass(frozen=True)
class ReviseLine:
    """변경 후 이 SKU의 **총 수량**. 증감분이 아니다.

    증감분을 받으면 같은 명령을 두 번 받을 때 수량이 두 배가 된다. 총 수량이면
    재전송이 무해하다 — 차이가 0이므로 아무것도 바뀌지 않는다. 24-1이 전체 내용을
    받아 fingerprint로 비교한 것과 같은 성격의 선택이다.
    """

    sku_code: str
    quantity: int


@dataclass(frozen=True)
class ReviseCommand:
    """권한 있는 판매사가 보낸 수량 변경 명령 (AC-03).

    요청을 판매사+외부 요청 ID로 지목한다. 내부 요청 id가 아니다. 그 ID가 판매사
    OMS가 소유한 식별자이며, 24-1이 내용 충돌을 CONFLICT로 거절할 때 "정식 리비전
    명령으로만 바꿀 수 있다"고 안내한 그 경로이기 때문이다. 응답을 잃은 판매사는
    내부 id를 모를 수 있지만 자기 외부 ID는 늘 안다.

    권한 확인은 하지 않는다. 판매사별 변경 권한자는 R-05/06이 미확정이고 AC-35가
    수동 확인을 전제하므로, 여기서는 `revised_by`를 기록만 하고 권한은 문서화된
    가정으로 둔다.

    기한 변경은 이 명령이 다루지 않는다. AC-23은 최초 기한을 보존하면서 유효
    기한만 바꾸라고 요구하며 별도 명령이다. SKU 구성 변경도 아니다 — NBR-06은
    취소 후 신규 요청으로 처리하라고 정했다.
    """

    seller_code: str
    external_request_id: str
    lines: tuple[ReviseLine, ...]
    revised_by: str
    reason: str
    occurred_at: datetime


def validate_revision(command: ReviseCommand) -> tuple[ValidationProblem, ...]:
    """수량 변경 명령의 유효성을 판정한다 (AC-03).

    현재 수량을 보지 않는다. 증감 어느 쪽인지는 조정 계층이 저장된 값과 비교해
    알아내며, 이 함수는 "이 입력 자체가 성립하는가"만 답한다.

    수량 0을 거절하는 것이 중요하다. 0은 그 SKU를 요청에서 빼는 것이고, NBR-06은
    SKU 구성 변경을 취소 후 신규 요청으로 처리하라고 정했다. 허용하면 라인을 0으로
    만들어 구성을 바꾸는 우회 경로가 생긴다.

    사유를 필수로 받는다. AC-03의 확인할 업무 근거에 "구간별 수요/배정/감소 사유"가
    있고, 증량인지 감량인지는 이 함수가 알 수 없으므로 조건을 걸지 않고 항상
    요구한다. 사유 없는 수량 변경은 기록이 아니라 빈칸이다.
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
        problems, "external_request_id", external_request_id, MAX_EXTERNAL_REQUEST_ID_LENGTH
    )

    if not command.lines:
        problems.append(ValidationProblem("lines", "변경할 품목이 없습니다"))

    codes = [normalize_text(line.sku_code) for line in command.lines]
    for index, (line, code) in enumerate(zip(command.lines, codes, strict=True)):
        if not code:
            problems.append(
                ValidationProblem(f"lines[{index}].sku_code", "SKU 코드가 비어 있습니다")
            )
        _check_length(problems, f"lines[{index}].sku_code", code, MAX_SKU_CODE_LENGTH)

        if line.quantity <= 0:
            # NBR-06. 0은 감량이 아니라 구성 변경이다.
            problems.append(
                ValidationProblem(
                    f"lines[{index}].quantity",
                    "수량은 양수여야 합니다. 품목을 빼려면 요청을 취소하고 다시 접수해야 합니다",
                )
            )

    # 같은 SKU가 두 번 오면 어느 수량이 최종인지 정해지지 않는다. uq_line_request_sku
    # 때문에 라인 자체도 SKU당 하나다.
    if len(set(codes)) != len(codes):
        problems.append(ValidationProblem("lines", "같은 품목이 두 번 지정됐습니다"))

    revised_by = normalize_text(command.revised_by)
    if not revised_by:
        problems.append(ValidationProblem("revised_by", "변경 담당자가 비어 있습니다"))
    _check_length(problems, "revised_by", revised_by, MAX_REVISED_BY_LENGTH)

    reason = normalize_text(command.reason)
    if not reason:
        problems.append(ValidationProblem("reason", "변경 사유가 비어 있습니다"))
    _check_length(problems, "reason", reason, MAX_REVISION_REASON_LENGTH)

    if command.occurred_at.tzinfo is None:
        problems.append(ValidationProblem("occurred_at", "시간대가 없는 변경 시각입니다"))

    return tuple(problems)


@dataclass(frozen=True)
class SegmentQuantity:
    """수요 구간 하나의 FIFO 순번과 요청 수량.

    배정량은 담지 않는다. 그것은 BC-04가 소유하는 사실이고 이 모듈은 알지 못한다.
    미충족 수량은 두 값을 가진 쪽이 빼서 구한다.
    """

    segment_id: uuid.UUID
    seq: int
    quantity: int


@dataclass(frozen=True)
class SegmentReduction:
    """한 수요 구간에서 줄일 수량."""

    segment_id: uuid.UUID
    quantity: int


def plan_demand_reduction(
    segments: Iterable[SegmentQuantity], shortfall: int
) -> tuple[SegmentReduction, ...]:
    """줄일 수량을 최신 구간부터 배분한다 (AC-03, D-01, POL-004).

    증량 배분이 순번 오름차순인 것과 **정확히 반대**다. 그래야 "수량 감소는 가장
    최근 추가분부터 줄인다"가 성립한다. 오래된 구간을 먼저 깎으면 선순위 수요가
    사라지고, 그 자리를 후순위가 차지하게 되어 FIFO가 뒤집힌다.

    배정 해제는 여기서 다루지 않는다. 구간 수량이 줄면 그 구간의 유효 배정이
    남는 수량을 넘을 수 있고, 넘는 만큼을 푸는 것은 BC-04의 일이다. 이 함수는
    수요가 얼마나 줄어드는지만 답한다.
    """
    remaining = shortfall
    plan: list[SegmentReduction] = []

    for segment in sorted(segments, key=lambda item: item.seq, reverse=True):
        if remaining <= 0:
            break
        taken = min(remaining, segment.quantity)
        if taken == 0:
            # 이미 0으로 줄어든 구간이다. 행은 이력으로 남아 있으므로 지나간다.
            continue
        plan.append(SegmentReduction(segment_id=segment.segment_id, quantity=taken))
        remaining -= taken

    return tuple(plan)

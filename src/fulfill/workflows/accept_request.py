"""출고 요청 접수 조정 (AC-01, AC-02).

판매사·SKU 해석(다른 모듈), 요청 검증(도메인), FIFO 순번 발급(allocation),
기록(requests)을 하나의 트랜잭션에서 묶는다.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fulfill.modules.allocation.guard import issue_demand_seqs
from fulfill.modules.inventory.queries import find_sku_ids_by_codes
from fulfill.modules.requests.domain import (
    IntakeCommand,
    fingerprint,
    validate,
)
from fulfill.modules.requests.repository import (
    ExistingRequest,
    NewRequest,
    find_existing,
    insert_request,
    is_duplicate_request_error,
)
from fulfill.modules.seller_contracts.queries import find_seller_id_by_code
from fulfill.shared.validation import ValidationProblem, normalize_text


class IntakeOutcome(StrEnum):
    """접수 명령의 판정 결과.

    21 §4의 결과 모델 중 접수 단계에 실제로 나타나는 것만 둔다.
    PENDING_REVIEW·UNKNOWN·INCIDENT는 실물 작업이나 외부 확인이 얽힐 때
    생기므로 여기에는 없다.
    """

    # 신규 요청으로 접수했다.
    APPLIED = "APPLIED"
    # 같은 ID·같은 내용의 재전달이라 기존 결과를 그대로 돌려준다.
    UNCHANGED = "UNCHANGED"
    # 같은 ID에 다른 내용이 왔다. 정식 리비전 명령으로만 바꿀 수 있다.
    CONFLICT = "CONFLICT"
    # 요청 자체가 유효하지 않다.
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class IntakeResult:
    """접수 판정과 그 근거.

    CONFLICT에도 request_id를 담는다. 판매사가 어느 기존 요청과 충돌했는지
    알아야 정정 경로를 찾을 수 있다. REJECTED에서는 아직 요청이 없으므로
    None이고 problems에 사유가 담긴다.
    """

    outcome: IntakeOutcome
    request_id: uuid.UUID | None = None
    problems: tuple[ValidationProblem, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class _Resolved:
    """코드를 id로 해석한 결과. 키는 정규화된 SKU 코드다."""

    seller_id: uuid.UUID
    sku_ids: dict[str, uuid.UUID]


def _resolve(session: Session, command: IntakeCommand) -> _Resolved | tuple[ValidationProblem, ...]:
    """판매사 코드와 SKU 코드를 실제 id로 해석한다.

    등록되지 않은 코드는 검증 실패와 같은 성격이다. 판매사가 읽을 수 있는 사유로
    돌려줘야 하므로 예외로 던지지 않는다.

    판매사를 찾지 못하면 바로 반환한다. SKU는 판매사 범위 안에서만 조회할 수
    있으므로 그 뒤 단계가 성립하지 않는다.
    """
    seller_id = find_seller_id_by_code(session, normalize_text(command.seller_code))
    if seller_id is None:
        return (ValidationProblem("seller_code", "등록되지 않은 판매사입니다"),)

    codes = [normalize_text(line.sku_code) for line in command.lines]
    sku_ids = find_sku_ids_by_codes(session, seller_id, codes)
    missing = tuple(
        ValidationProblem(f"lines[{index}].sku_code", "이 판매사에 등록되지 않은 SKU입니다")
        for index, code in enumerate(codes)
        if code not in sku_ids
    )
    if missing:
        return missing

    return _Resolved(seller_id=seller_id, sku_ids=sku_ids)


def _outcome_for_existing(existing: ExistingRequest, fingerprint: str) -> IntakeResult:
    """이미 있는 요청과 비교해 재전달인지 충돌인지 가린다 (AC-02).

    삽입 전 조회와, 유니크 위반 뒤 재조회 두 곳에서 같은 판정이 필요하다.
    """
    if existing.fingerprint == fingerprint:
        return IntakeResult(IntakeOutcome.UNCHANGED, request_id=existing.id)
    return IntakeResult(IntakeOutcome.CONFLICT, request_id=existing.id)


def _write_new_request(
    session: Session,
    command: IntakeCommand,
    resolved: _Resolved,
    content_fingerprint: str,
    received_at: datetime,
) -> IntakeResult:
    """순번을 발급하고 요청을 기록한다. 동시 접수에 밀리면 기존 결과를 돌려준다.

    세이브포인트로 감싸는 이유: 유니크 위반이 나면 PostgreSQL이 트랜잭션 전체를
    중단 상태로 만들어 더 이상 읽을 수 없게 된다. 세이브포인트까지만 되돌리면
    바깥 트랜잭션을 계속 쓸 수 있고, 되돌릴 때 발급한 FIFO 순번도 함께 사라져
    결번이 남지 않는다.
    """
    sku_ids = [resolved.sku_ids[normalize_text(line.sku_code)] for line in command.lines]
    try:
        with session.begin_nested():
            demand_seqs = issue_demand_seqs(session, sku_ids)
            request_id = insert_request(
                session,
                NewRequest(
                    seller_id=resolved.seller_id,
                    command=command,
                    fingerprint=content_fingerprint,
                    sku_ids=resolved.sku_ids,
                    demand_seqs=demand_seqs,
                    received_at=received_at,
                ),
            )
    except IntegrityError as error:
        if not is_duplicate_request_error(error):
            raise
        # 유니크 위반이 났다는 것은 상대가 이미 커밋했다는 뜻이다. 커밋 전이었다면
        # 우리는 오류가 아니라 인덱스에서 대기했을 것이다. 그래서 지금 다시 읽으면
        # 그 행이 보인다(READ COMMITTED 기준).
        existing = find_existing(
            session, resolved.seller_id, normalize_text(command.external_request_id)
        )
        if existing is None:
            raise
        return _outcome_for_existing(existing, content_fingerprint)

    return IntakeResult(IntakeOutcome.APPLIED, request_id=request_id)


def accept_request(session: Session, command: IntakeCommand, received_at: datetime) -> IntakeResult:
    """출고 요청을 접수한다 (AC-01, AC-02).

    순서에 이유가 있다. 검증은 DB가 필요 없으므로 가장 먼저 한다. 그다음 참조를
    해석하고, 쓰기 전에 기존 요청을 조회한다. 재전달은 이 지점에서 끝나므로
    잠금과 순번 발급까지 가지 않는다.

    재고는 어디서도 보지 않는다. 재고가 없는 요청도 유효하게 접수되어 대기한다
    (NBR-01, AC-01).
    """
    problems = validate(command)
    if problems:
        return IntakeResult(IntakeOutcome.REJECTED, problems=problems)

    resolved = _resolve(session, command)
    if isinstance(resolved, tuple):
        return IntakeResult(IntakeOutcome.REJECTED, problems=resolved)

    content_fingerprint = fingerprint(command)
    existing = find_existing(
        session, resolved.seller_id, normalize_text(command.external_request_id)
    )
    if existing is not None:
        return _outcome_for_existing(existing, content_fingerprint)

    return _write_new_request(session, command, resolved, content_fingerprint, received_at)

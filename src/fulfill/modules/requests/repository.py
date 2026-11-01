"""requests 모듈의 영속화 (BC-02).

ORM 객체를 모듈 밖으로 내보내지 않는다. 호출자에게는 판정에 필요한 값만 담은
평범한 데이터 구조를 돌려준다.
"""

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fulfill.modules.requests.domain import (
    NORMALIZATION_VERSION,
    IntakeCommand,
    ReviseCommand,
    SegmentQuantity,
)
from fulfill.modules.requests.models import (
    DEMAND_KIND_INCREASE,
    DEMAND_KIND_ORIGINAL,
    DemandSegment,
    FulfillmentRequest,
    RequestLine,
    RequestRevision,
)
from fulfill.shared.validation import normalize_text

# 동시 접수를 실제로 막는 제약의 이름. 중복 판별이 이 이름에 의존한다.
UNIQUE_REQUEST_CONSTRAINT = "uq_request_seller_external_id"


@dataclass(frozen=True)
class ExistingRequest:
    """이미 접수된 요청에서 재전달 판정에 필요한 값만 추린 것."""

    id: uuid.UUID
    fingerprint: str


def find_existing(
    session: Session, seller_id: uuid.UUID, external_request_id: str
) -> ExistingRequest | None:
    """판매사+외부 요청 ID로 기존 요청을 찾는다. None이면 최초 접수다."""
    row = session.execute(
        select(FulfillmentRequest.id, FulfillmentRequest.fingerprint).where(
            FulfillmentRequest.seller_id == seller_id,
            FulfillmentRequest.external_request_id == external_request_id,
        )
    ).one_or_none()
    if row is None:
        return None
    return ExistingRequest(id=row[0], fingerprint=row[1])


def is_duplicate_request_error(error: IntegrityError) -> bool:
    """이 오류가 동시 접수로 인한 중복인지 판별한다.

    제약 이름을 확인하는 이유가 있다. 모든 IntegrityError를 중복으로 보면 FK
    위반 같은 진짜 오류를 UNCHANGED로 숨기게 되고, 잘못된 요청이 정상 접수된
    것처럼 보인다.

    psycopg는 위반된 제약 이름을 진단 정보로 제공한다. 그것을 얻지 못하는
    경우에만 메시지 문자열로 떨어뜨린다.
    """
    original = error.orig
    diagnostics: object = getattr(original, "diag", None)
    constraint: object = getattr(diagnostics, "constraint_name", None)
    if isinstance(constraint, str):
        return constraint == UNIQUE_REQUEST_CONSTRAINT
    return UNIQUE_REQUEST_CONSTRAINT in str(original)


@dataclass(frozen=True)
class NewRequest:
    """최초 접수를 기록하는 데 필요한 값 묶음.

    sku_ids는 inventory가, demand_seqs는 allocation이 해석한 결과다. 이 모듈은
    두 값을 받아 쓰기만 하고 직접 구하지 않는다.
    """

    seller_id: uuid.UUID
    command: IntakeCommand
    fingerprint: str
    sku_ids: Mapping[str, uuid.UUID]
    demand_seqs: Mapping[uuid.UUID, int]
    received_at: datetime


def _build_request_row(new: NewRequest) -> FulfillmentRequest:
    """저장할 FR 행을 만든다.

    텍스트는 정규화된 값으로 저장한다. fingerprint도 같은 값으로 계산했으므로
    저장값과 비교값이 어긋나지 않는다.

    접수 시점에는 최초 기한과 유효 기한이 같다. 이후 기한 재협의는 별도 권한의
    리비전이 유효 기한만 바꾸고 최초 기한은 보존한다(AC-23).
    """
    command = new.command
    return FulfillmentRequest(
        seller_id=new.seller_id,
        external_request_id=normalize_text(command.external_request_id),
        fingerprint=new.fingerprint,
        normalization_version=NORMALIZATION_VERSION,
        recipient_name=normalize_text(command.recipient.name),
        recipient_address=normalize_text(command.recipient.address),
        recipient_phone=normalize_text(command.recipient.phone),
        desired_ship_date=command.desired_ship_date,
        initial_due_at=command.initial_due_at,
        effective_due_at=command.initial_due_at,
        occurred_at=command.occurred_at,
        received_at=new.received_at,
    )


def insert_request(session: Session, new: NewRequest) -> uuid.UUID:
    """최초 접수를 기록하고 FR id를 돌려준다.

    유니크 제약 위반은 잡지 않고 그대로 올린다. 그것을 "다른 쪽이 먼저
    성공했다"로 해석해 재조회할지는 조정 계층의 판단이며, 저장 계층이 대신
    결정하면 진짜 오류까지 삼키게 된다.
    """
    request = _build_request_row(new)
    session.add(request)
    # 라인이 FR id를 참조하므로 먼저 id를 확정한다.
    session.flush()

    lines = [
        RequestLine(
            request_id=request.id,
            sku_id=new.sku_ids[normalize_text(line.sku_code)],
            quantity=line.quantity,
        )
        for line in new.command.lines
    ]
    session.add_all(lines)
    # 수요 구간이 라인 id를 참조하므로 여기서 한 번 더 확정한다.
    session.flush()

    session.add_all(
        DemandSegment(
            line_id=row.id,
            sku_id=row.sku_id,
            seq=new.demand_seqs[row.sku_id],
            quantity=row.quantity,
            kind=DEMAND_KIND_ORIGINAL,
        )
        for row in lines
    )
    return request.id


@dataclass(frozen=True)
class LineState:
    """수량 변경을 판정하는 데 필요한 라인 하나의 현재 상태."""

    line_id: uuid.UUID
    sku_id: uuid.UUID
    quantity: int
    segments: tuple[SegmentQuantity, ...]


def lock_lines_by_sku(
    session: Session, request_id: uuid.UUID, sku_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, LineState]:
    """요청의 해당 라인들을 잠그고 현재 수량·수요 구간을 돌려준다 (AC-03).

    요청에 없는 SKU는 결과에서 빠진다. 호출자는 그것으로 잘못된 변경 대상을
    알아낸다.

    라인을 잠그는 이유가 guard 잠금과 다르다. guard는 그 SKU의 배정 판정 전체를
    직렬화하고, 이 잠금은 라인의 현재 수량을 지킨다. 읽은 수량으로 증감 차이를
    계산한 뒤 쓰므로, 그 사이에 값이 바뀌면 차이가 틀린 값이 된다.

    구간은 잠그지 않는다. 같은 SKU의 구간을 건드리는 경로가 모두 guard를 지나므로
    이미 직렬화돼 있다.
    """
    sku_id_list = list(sku_ids)
    rows = session.execute(
        select(RequestLine.id, RequestLine.sku_id, RequestLine.quantity)
        .where(RequestLine.request_id == request_id, RequestLine.sku_id.in_(sku_id_list))
        .order_by(RequestLine.sku_id)
        .with_for_update()
    ).all()

    segments = session.execute(
        select(
            DemandSegment.line_id,
            DemandSegment.id,
            DemandSegment.seq,
            DemandSegment.quantity,
        ).where(DemandSegment.line_id.in_([row[0] for row in rows]))
    ).all()

    by_line: dict[uuid.UUID, list[SegmentQuantity]] = {}
    for line_id, segment_id, seq, quantity in segments:
        by_line.setdefault(line_id, []).append(
            SegmentQuantity(segment_id=segment_id, seq=seq, quantity=quantity)
        )

    return {
        row[1]: LineState(
            line_id=row[0],
            sku_id=row[1],
            quantity=row[2],
            segments=tuple(by_line.get(row[0], ())),
        )
        for row in rows
    }


def insert_increase_segment(
    session: Session, line_id: uuid.UUID, sku_id: uuid.UUID, seq: int, quantity: int
) -> None:
    """증량분을 새 수요 구간으로 만든다 (AC-03, NBR-05, POL-004).

    기존 구간의 수량을 늘리지 않는 것이 규칙의 핵심이다. 늘리면 증량분이 원수요의
    순번을 물려받아 다른 요청의 선순위를 앞지르고, 그것이 AC-03의 위반 조항이다.
    새 순번은 호출자가 allocation guard에서 발급받아 넘긴다.
    """
    session.add(
        DemandSegment(
            line_id=line_id,
            sku_id=sku_id,
            seq=seq,
            quantity=quantity,
            kind=DEMAND_KIND_INCREASE,
        )
    )


def reduce_segment(session: Session, segment_id: uuid.UUID, quantity: int) -> None:
    """수요 구간의 수량을 줄인다 (AC-03, D-01).

    행을 지우지 않는다. allocation이 이 구간을 참조하고 있고, 지우면 AC-36이
    요구하는 배정 이력을 잃는다. 0으로 남는 구간은 정상이며
    ck_segment_quantity_not_negative가 그것을 허용한다.
    """
    session.execute(
        update(DemandSegment)
        .where(DemandSegment.id == segment_id)
        .values(quantity=DemandSegment.quantity - quantity)
    )


def update_line_quantity(session: Session, line_id: uuid.UUID, quantity: int) -> None:
    """라인의 유효 수량을 갱신한다.

    구간 수량의 합과 같아야 한다. 어긋나면 요청이 요구하는 수량과 FIFO에 걸린
    수량이 달라지므로, 그것을 확인하는 통합 테스트를 둔다.
    """
    session.execute(update(RequestLine).where(RequestLine.id == line_id).values(quantity=quantity))


@dataclass(frozen=True)
class QuantityChange:
    """라인 하나의 이전·이후 수량."""

    line_id: uuid.UUID
    previous_quantity: int
    new_quantity: int


def insert_revisions(
    session: Session,
    request_id: uuid.UUID,
    group_id: uuid.UUID,
    changes: Iterable[QuantityChange],
    command: ReviseCommand,
) -> None:
    """수량 변경을 사실로 기록한다 (AC-03, AC-36).

    바뀐 라인마다 한 행이고, 한 명령이 바꾼 행들은 group_id를 공유한다. 수요
    구간과 라인 수량은 갱신되지만 "누가 언제 무엇을 왜 바꿨는가"는 지워지지
    않는다.
    """
    session.add_all(
        RequestRevision(
            group_id=group_id,
            request_id=request_id,
            line_id=change.line_id,
            previous_quantity=change.previous_quantity,
            new_quantity=change.new_quantity,
            revised_by=normalize_text(command.revised_by),
            reason=normalize_text(command.reason),
            occurred_at=command.occurred_at,
        )
        for change in changes
    )

"""BC-02가 공개하는 조회 입구.

다른 모듈은 `models`를 import하지 않고 이 함수들을 호출한다. 19 §2가 BC-04에
대해 "DemandSegment는 BC-02가 소유하며 공개 입구로 읽는다"고 정한 그 입구다.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from fulfill.modules.requests.domain import SegmentQuantity
from fulfill.modules.requests.models import DemandSegment, FulfillmentRequest, RequestLine


def list_demand_segments(session: Session, sku_id: uuid.UUID) -> tuple[SegmentQuantity, ...]:
    """이 SKU에 걸린 모든 수요 구간을 돌려준다 (AC-03).

    판매사로 다시 좁히지 않는다. SKU가 이미 정확히 한 판매사에 속하므로 sku_id가
    곧 판매사+SKU 큐를 지정한다. guard의 PK가 sku_id 하나인 것과 같은 이유다.

    정렬하지 않는다. 일부러 그렇게 둔다. 순번으로 정렬해 돌려주면 FIFO 순서를
    지키는 책임이 이 조회문의 ORDER BY로 옮겨 가고, 나중에 조회를 수정하는 사람이
    규칙을 깨면서도 그 사실을 알지 못한다. 정렬은 `plan_allocations`가 한다.

    전량 배정된 구간도 함께 돌려준다. 걸러낼 수 있는 쪽은 배정량을 아는 호출자이고,
    `plan_allocations`는 미충족이 0인 구간을 그냥 지나간다.
    """
    rows = session.execute(
        select(DemandSegment.id, DemandSegment.seq, DemandSegment.quantity).where(
            DemandSegment.sku_id == sku_id
        )
    ).all()
    return tuple(SegmentQuantity(segment_id=row[0], seq=row[1], quantity=row[2]) for row in rows)


@dataclass(frozen=True)
class LineDemand:
    """요청 라인 하나의 품목·수량과 그 라인에 달린 수요 구간들.

    `quantity`와 구간 수량의 합은 보통 같지만 반드시 같다고 보지 않는다. 증량이
    일어나면 라인 수량이 늘고 새 구간이 생기므로 둘을 함께 들고 있어야 어긋났을
    때 드러난다.
    """

    sku_id: uuid.UUID
    quantity: int
    segments: tuple[SegmentQuantity, ...]


def summarize_request_demand(
    session: Session, request_id: uuid.UUID
) -> tuple[LineDemand, ...] | None:
    """요청 하나의 라인별 수요를 돌려준다. None이면 그런 요청이 없다 (AC-04).

    품목 코드는 담지 않는다. Sku는 BC-03의 소유이므로 이 모듈이 읽을 수 없고,
    코드로 바꾸는 일은 두 모듈을 함께 보는 조정 계층이 한다.

    라인이 없는 요청과 존재하지 않는 요청을 구분한다. 접수 검증이 라인을
    요구하므로 전자는 생기지 않지만, 둘을 뭉개면 없는 요청에 200을 주게 된다.
    """
    exists = session.execute(
        select(FulfillmentRequest.id).where(FulfillmentRequest.id == request_id)
    ).scalar_one_or_none()
    if exists is None:
        return None

    rows = session.execute(
        select(RequestLine.id, RequestLine.sku_id, RequestLine.quantity).where(
            RequestLine.request_id == request_id
        )
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

    return tuple(
        LineDemand(
            sku_id=row[1],
            quantity=row[2],
            # 여기서는 순번으로 정렬한다. 사람이 읽을 현황이므로 순서가 보여야
            # 하고, 배정 판정에 쓰이는 값이 아니다.
            segments=tuple(sorted(by_line.get(row[0], ()), key=lambda item: item.seq)),
        )
        for row in rows
    )

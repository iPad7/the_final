"""출고 요청 접수 HTTP 입구 (BC-02).

JSON은 camelCase를 쓴다. 외부 업무 계약(21 §3.1)이 `externalRequestId` 같은
camelCase 식별자를 쓰므로 그 표기를 따르고, Python 쪽은 snake_case를 유지한다.
"""

import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from fulfill.modules.requests.domain import (
    IntakeCommand,
    Recipient,
    RequestedLine,
    ReviseCommand,
    ReviseLine,
)
from fulfill.shared.db import unit_of_work
from fulfill.shared.http import to_camel_path
from fulfill.workflows.accept_request import IntakeOutcome, IntakeResult, accept_request
from fulfill.workflows.revise_demand import RevisionOutcome, revise_demand


class _Payload(BaseModel):
    """DTO 공통 설정. JSON은 camelCase, 파이썬 속성은 snake_case."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# UNCHANGED가 200인 것이 중요하다. 아무것도 생성되지 않았으므로 201이면 거짓이
# 되고, 클라이언트가 상태코드만으로 신규 생성 여부를 구분할 수 없게 된다.
# REJECTED는 422를 쓴다. JSON 자체는 정상이고 업무상 받을 수 없다는 뜻이다.
_STATUS_BY_OUTCOME = {
    IntakeOutcome.APPLIED: status.HTTP_201_CREATED,
    IntakeOutcome.UNCHANGED: status.HTTP_200_OK,
    IntakeOutcome.CONFLICT: status.HTTP_409_CONFLICT,
    IntakeOutcome.REJECTED: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


class LinePayload(_Payload):
    """요청 라인. 수량 제약은 일부러 걸지 않는다.

    `quantity > 0` 같은 규칙을 여기 걸면 도메인 `validate`와 같은 규칙이 두 곳에
    생기고, 판매사는 업무 사유 대신 스키마 오류 메시지를 받는다. 업무 규칙의
    주인은 도메인 하나로 둔다.
    """

    sku_code: str
    quantity: int


class RecipientPayload(_Payload):
    name: str
    address: str
    phone: str


class IntakePayload(_Payload):
    """판매사 OMS가 보내는 출고 지시 본문.

    occurredAt은 판매사가 지시를 발행한 시각이라 본문으로 받는다. 수신 시각은
    3PL이 관찰하는 사실이므로 클라이언트가 주장할 수 없고 서버가 정한다.
    """

    seller_code: str
    external_request_id: str
    lines: list[LinePayload]
    recipient: RecipientPayload
    initial_due_at: datetime
    occurred_at: datetime
    desired_ship_date: date | None = None


class ProblemPayload(_Payload):
    field: str
    reason: str


class IntakeResponse(_Payload):
    """접수 판정 응답.

    사유를 응답에 담는 이유: AC-01이 업무상 오류 사유를 확인 가능하게 거절하라고
    요구한다. 거절이 왜 났는지 판매사가 읽을 수 없으면 그 조건을 만족하지 못한다.
    """

    outcome: IntakeOutcome
    request_id: uuid.UUID | None = None
    # Pydantic은 가변 기본값을 인스턴스마다 복사하므로 그대로 둬도 공유되지 않는다.
    problems: list[ProblemPayload] = []


def _to_command(payload: IntakePayload) -> IntakeCommand:
    """DTO를 도메인 명령으로 옮긴다.

    도메인이 Pydantic이나 FastAPI를 알아서는 안 되므로 변환을 여기서 한다. DTO는
    HTTP 계약이고 IntakeCommand는 업무 어휘라서 서로 따로 바뀔 수 있다.
    """
    return IntakeCommand(
        seller_code=payload.seller_code,
        external_request_id=payload.external_request_id,
        lines=tuple(
            RequestedLine(sku_code=line.sku_code, quantity=line.quantity) for line in payload.lines
        ),
        recipient=Recipient(
            name=payload.recipient.name,
            address=payload.recipient.address,
            phone=payload.recipient.phone,
        ),
        initial_due_at=payload.initial_due_at,
        desired_ship_date=payload.desired_ship_date,
        occurred_at=payload.occurred_at,
    )


def _to_response(result: IntakeResult) -> IntakeResponse:
    """조정 계층의 판정을 응답 DTO로 옮긴다."""
    return IntakeResponse(
        outcome=result.outcome,
        request_id=result.request_id,
        problems=[
            ProblemPayload(field=to_camel_path(problem.field), reason=problem.reason)
            for problem in result.problems
        ],
    )


router = APIRouter(tags=["fulfillment-requests"])


@router.post("/fulfillment-requests")
def accept_fulfillment_request(payload: IntakePayload, response: Response) -> IntakeResponse:
    """판매사 OMS의 출고 지시를 접수한다 (AC-01, AC-02)."""
    # 트랜잭션을 FastAPI 의존성(yield)으로 빼지 않는다. 의존성의 종료 코드는 응답이
    # 전송된 뒤에 실행되므로, 거기서 커밋이 실패하면 클라이언트는 롤백된 작업에
    # 대해 이미 201을 받은 상태가 된다. 블록을 엔드포인트 안에서 닫아 커밋이 끝난
    # 뒤에 상태코드를 정한다.
    with unit_of_work() as session:
        result = accept_request(
            session,
            _to_command(payload),
            received_at=datetime.now(UTC),
        )

    response.status_code = _STATUS_BY_OUTCOME[result.outcome]
    return _to_response(result)


# APPLIED가 201인 이유는 변경 사실 행이 새로 생기기 때문이다. UNCHANGED를 같은
# 200으로 두면 판매사가 상태코드만으로 적용 여부를 구분할 수 없게 되는데, 24-1의
# 재전달에서 같은 이유로 두 코드를 갈라 놨다.
_STATUS_BY_REVISION_OUTCOME = {
    RevisionOutcome.APPLIED: status.HTTP_201_CREATED,
    RevisionOutcome.UNCHANGED: status.HTTP_200_OK,
    RevisionOutcome.REJECTED: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


class ReviseLinePayload(_Payload):
    """변경 후 이 SKU의 총 수량. 증감분이 아니다.

    증감분을 받으면 재전송이 수량을 두 배로 만든다. 총 수량이면 같은 명령을 두 번
    받아도 두 번째는 UNCHANGED로 끝난다.
    """

    sku_code: str
    quantity: int


class RevisionPayload(_Payload):
    """권한 있는 판매사가 보내는 수량 변경 명령.

    요청을 판매사+외부 요청 ID로 지목한다. 내부 요청 id가 아니다. 24-1이 내용
    충돌을 CONFLICT로 거절할 때 "정식 리비전 명령으로만 바꿀 수 있다"고 안내한
    경로이며, 응답을 잃은 판매사는 내부 id를 모를 수 있지만 자기 외부 ID는 안다.
    """

    seller_code: str
    external_request_id: str
    lines: list[ReviseLinePayload]
    revised_by: str
    reason: str
    occurred_at: datetime


class RevisionResponse(_Payload):
    """수량 변경 판정 응답.

    풀린 수량과 재배정된 수량을 나눠 담는다. 합치면 "줄인 만큼 풀린 것"과 "다른
    요청으로 옮겨 간 것"을 구분할 수 없다.
    """

    outcome: RevisionOutcome
    request_id: uuid.UUID | None = None
    released_quantity: int = 0
    reallocated_quantity: int = 0
    problems: list[ProblemPayload] = []


@router.post("/fulfillment-request-revisions")
def revise_fulfillment_request(payload: RevisionPayload, response: Response) -> RevisionResponse:
    """접수된 요청의 SKU 수량을 변경한다 (AC-03).

    증량은 변경 시점의 새 FIFO 구간이 되고, 감량은 최신 구간부터 줄어든다. 감량으로
    풀린 물량은 같은 트랜잭션에서 그 시점의 선순위부터 다시 배정된다(CB-03).
    """
    with unit_of_work() as session:
        result = revise_demand(
            session,
            ReviseCommand(
                seller_code=payload.seller_code,
                external_request_id=payload.external_request_id,
                lines=tuple(
                    ReviseLine(sku_code=line.sku_code, quantity=line.quantity)
                    for line in payload.lines
                ),
                revised_by=payload.revised_by,
                reason=payload.reason,
                occurred_at=payload.occurred_at,
            ),
        )

    response.status_code = _STATUS_BY_REVISION_OUTCOME[result.outcome]
    return RevisionResponse(
        outcome=result.outcome,
        request_id=result.request_id,
        released_quantity=result.released_quantity,
        reallocated_quantity=result.reallocated_quantity,
        problems=[
            ProblemPayload(field=to_camel_path(problem.field), reason=problem.reason)
            for problem in result.problems
        ],
    )

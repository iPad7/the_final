"""출고 요청 접수 HTTP 입구 (BC-02).

JSON은 camelCase를 쓴다. 외부 업무 계약(21 §3.1)이 `externalRequestId` 같은
camelCase 식별자를 쓰므로 그 표기를 따르고, Python 쪽은 snake_case를 유지한다.
"""

import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from fulfill.modules.requests.domain import IntakeCommand, Recipient, RequestedLine
from fulfill.shared.db import unit_of_work
from fulfill.shared.http import to_camel_path
from fulfill.workflows.accept_request import IntakeOutcome, IntakeResult, accept_request


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

"""입고·검수·귀속 확인 HTTP 입구 (BC-03).

JSON은 camelCase를 쓴다. 24-1의 출고 요청 접수와 같은 규약이며, 한 API 안에서
표기가 갈리면 쓰는 쪽이 매번 확인해야 한다.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Response, status
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from fulfill.modules.inventory.domain import (
    QC_STATE_FAILED,
    QC_STATE_PASSED,
    QC_STATE_PENDING,
    AdviceCommand,
    OwnershipReviewCommand,
    QcCommand,
    ReceiptCommand,
)
from fulfill.modules.inventory.queries import (
    AdviceSummary,
    ReceiptSummary,
    summarize_advice,
    summarize_receipt,
)
from fulfill.shared.db import unit_of_work
from fulfill.shared.http import to_camel_path
from fulfill.workflows.receiving import (
    ReceivingOutcome,
    ReceivingResult,
    record_inbound_advice,
    record_qc_decision,
    record_receipt,
    review_ownership,
)


class _Payload(BaseModel):
    """DTO 공통 설정. JSON은 camelCase, 파이썬 속성은 snake_case."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# REJECTED에 422를 쓰는 이유는 24-1과 같다. JSON 자체는 정상이고 업무상 받을 수
# 없다는 뜻이다. 400으로 하면 쓰는 쪽이 "요청을 잘못 만들었다"로 읽고 재시도
# 로직을 잘못 짠다.
_STATUS_BY_OUTCOME = {
    ReceivingOutcome.APPLIED: status.HTTP_201_CREATED,
    ReceivingOutcome.REJECTED: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


class ProblemPayload(_Payload):
    field: str
    reason: str


class ReceivingResponse(_Payload):
    """입고·검수 명령의 판정 응답.

    사유를 담는 이유: AC-06·07·08이 업무 근거를 확인 가능하게 요구한다. 거절이
    왜 났는지 담당자가 읽을 수 없으면 그 조건을 만족하지 못한다.
    """

    outcome: ReceivingOutcome
    receipt_id: uuid.UUID | None = None
    advice_id: uuid.UUID | None = None
    problems: list[ProblemPayload] = []


def _to_response(result: ReceivingResult) -> ReceivingResponse:
    """조정 계층의 판정을 응답 DTO로 옮긴다."""
    return ReceivingResponse(
        outcome=result.outcome,
        receipt_id=result.receipt_id,
        advice_id=result.advice_id,
        problems=[
            ProblemPayload(field=to_camel_path(problem.field), reason=problem.reason)
            for problem in result.problems
        ],
    )


router = APIRouter(tags=["receiving"])

# 경로 파라미터도 외부 계약이므로 camelCase로 노출하고 파이썬 쪽은 snake_case를
# 유지한다. 본문은 camelCase인데 경로만 snake_case면 쓰는 쪽이 매번 확인해야 한다.
ReceiptId = Annotated[uuid.UUID, Path(alias="receiptId")]
AdviceId = Annotated[uuid.UUID, Path(alias="adviceId")]


class AdvicePayload(_Payload):
    """판매사가 보내는 입고 예정 통보.

    판매사·품목이 필수인 점이 실도착과 다르다. 통보를 보낸 쪽이 누구인지는 늘
    알려져 있다.
    """

    seller_code: str
    sku_code: str
    expected_quantity: int
    advised_at: datetime


@router.post("/inbound-advices")
def accept_inbound_advice(payload: AdvicePayload, response: Response) -> ReceivingResponse:
    """입고 예정을 접수한다 (AC-06).

    재고 버킷을 만들지 않는다. 예정량을 물리 정상 재고로 취급하지 않는다는
    AC-06의 요구가 이 사실로 성립한다.
    """
    with unit_of_work() as session:
        result = record_inbound_advice(
            session,
            AdviceCommand(
                seller_code=payload.seller_code,
                sku_code=payload.sku_code,
                expected_quantity=payload.expected_quantity,
                advised_at=payload.advised_at,
            ),
        )

    response.status_code = _STATUS_BY_OUTCOME[result.outcome]
    return _to_response(result)


class ReceiptPayload(_Payload):
    """입고 담당자가 확인한 실도착.

    sellerCode와 skuCode가 선택인 것이 AC-08이 HTTP 계약에 드러나는 자리다.
    소유자를 모르는 물건의 도착을 보낼 수 있어야 하며, 필수로 두면 미확인 입고를
    기록할 방법이 API에 없다.

    수량 제약은 일부러 걸지 않는다. 업무 규칙의 주인은 도메인 하나이며, 여기
    걸면 담당자가 업무 사유 대신 스키마 오류 메시지를 받는다.
    """

    arrived_quantity: int
    confirmed_by: str
    arrived_at: datetime
    seller_code: str | None = None
    sku_code: str | None = None
    advice_id: uuid.UUID | None = None


@router.post("/receipts")
def accept_receipt(payload: ReceiptPayload, response: Response) -> ReceivingResponse:
    """실도착을 접수한다 (AC-06, AC-08)."""
    # 트랜잭션을 FastAPI 의존성(yield)으로 빼지 않는다. 의존성의 종료 코드는 응답이
    # 전송된 뒤에 실행되므로, 거기서 커밋이 실패하면 클라이언트는 롤백된 작업에
    # 대해 이미 201을 받은 상태가 된다.
    with unit_of_work() as session:
        result = record_receipt(
            session,
            ReceiptCommand(
                arrived_quantity=payload.arrived_quantity,
                confirmed_by=payload.confirmed_by,
                arrived_at=payload.arrived_at,
                seller_code=payload.seller_code,
                sku_code=payload.sku_code,
                advice_id=payload.advice_id,
            ),
        )

    response.status_code = _STATUS_BY_OUTCOME[result.outcome]
    return _to_response(result)


class BucketPayload(_Payload):
    qc_state: str
    location_code: str | None
    quantity: int


class ReceiptSummaryResponse(_Payload):
    """입고 하나의 검수 현황 (AC-07).

    세 수량을 한 응답에 담는다. 따로 조회하게 두면 합격만 보고 미검수를 놓칠 수
    있고, 그것이 "불량을 가용으로 계산"이라는 AC-07의 위반 조항이다.

    bucketTotal을 함께 내보내는 이유: 귀속이 확인된 입고에서 이 값이 arrivedQuantity와
    같아야 한다는 것이 재고 수량 보존 조건이다. 밖에서 확인할 수 있어야 한다.
    """

    arrived_quantity: int
    ownership_status: str
    pending_quantity: int
    passed_quantity: int
    failed_quantity: int
    bucket_total: int
    buckets: list[BucketPayload]


def _to_receipt_summary(summary: ReceiptSummary) -> ReceiptSummaryResponse:
    return ReceiptSummaryResponse(
        arrived_quantity=summary.arrived_quantity,
        ownership_status=summary.ownership_status,
        pending_quantity=summary.quantity_in(QC_STATE_PENDING),
        passed_quantity=summary.quantity_in(QC_STATE_PASSED),
        failed_quantity=summary.quantity_in(QC_STATE_FAILED),
        bucket_total=summary.bucket_total,
        buckets=[
            BucketPayload(
                qc_state=bucket.qc_state,
                location_code=bucket.location_code,
                quantity=bucket.quantity,
            )
            for bucket in summary.buckets
        ],
    )


@router.get("/receipts/{receiptId}")
def read_receipt(receipt_id: ReceiptId) -> ReceiptSummaryResponse:
    """입고의 검수 현황을 돌려준다 (AC-07)."""
    with unit_of_work() as session:
        summary = summarize_receipt(session, receipt_id)
    if summary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="등록되지 않은 입고입니다"
        )
    return _to_receipt_summary(summary)


class AdviceSummaryResponse(_Payload):
    """예정 입고 하나의 현황 (AC-06).

    예정량·실도착 합계·부족분을 함께 돌려준다. AC-06이 "실도착 590과 예정 대비
    부족 10을 구별"하라고 요구하므로 둘을 뭉개 쓸 수 없게 셋을 다 담는다.
    """

    expected_quantity: int
    arrived_quantity: int
    shortfall: int


@router.get("/inbound-advices/{adviceId}")
def read_inbound_advice(advice_id: AdviceId) -> AdviceSummaryResponse:
    """예정 대비 실도착 현황을 돌려준다 (AC-06)."""
    with unit_of_work() as session:
        summary: AdviceSummary | None = summarize_advice(session, advice_id)
    if summary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="등록되지 않은 입고 예정입니다"
        )
    return AdviceSummaryResponse(
        expected_quantity=summary.expected_quantity,
        arrived_quantity=summary.arrived_quantity,
        shortfall=summary.shortfall,
    )


class QcDecisionPayload(_Payload):
    """검수자가 내린 판정 하나.

    receiptId는 경로에서 받으므로 본문에 없다. 양쪽에서 받으면 두 값이 어긋날 수
    있고 어느 쪽이 맞는지 정하는 규칙이 또 필요해진다.

    locationCode는 불합격품을 둘 격리 구역이다. 합격 판정에서는 물건이 움직이지
    않으므로 쓰이지 않는다.
    """

    result: str
    quantity: int
    decided_by: str
    occurred_at: datetime
    location_code: str | None = None


@router.post("/receipts/{receiptId}/qc-decisions")
def accept_qc_decision(
    receipt_id: ReceiptId, payload: QcDecisionPayload, response: Response
) -> ReceivingResponse:
    """검수 판정을 접수한다 (AC-07).

    입고의 하위 자원인 이유: 판정은 입고 없이 존재할 수 없고, 한 입고에 판정이
    여러 개 쌓이는 것이 부분 QC다.
    """
    with unit_of_work() as session:
        outcome = record_qc_decision(
            session,
            QcCommand(
                receipt_id=receipt_id,
                result=payload.result,
                quantity=payload.quantity,
                decided_by=payload.decided_by,
                occurred_at=payload.occurred_at,
                location_code=payload.location_code,
            ),
        )

    response.status_code = _STATUS_BY_OUTCOME[outcome.outcome]
    return _to_response(outcome)


class OwnershipReviewPayload(_Payload):
    """귀속을 확인하려 한 시도 하나.

    sellerCode·skuCode는 확인에 성공했을 때만 채운다. note는 확인하지 못한 경우에
    필수이며, 사유 없는 "확인 못 함"은 AC-08의 업무 근거가 되지 못한다.

    dueAt은 이 시도에 적용한 운영사 공통 확인 기한이다. 그 값은 아직 미확정 운영
    파라미터(R-05/06)이므로 서버가 만들지 않고 받아 기록한다.
    """

    outcome: str
    reviewed_by: str
    occurred_at: datetime
    seller_code: str | None = None
    sku_code: str | None = None
    note: str | None = None
    due_at: datetime | None = None


@router.post("/receipts/{receiptId}/ownership-reviews")
def accept_ownership_review(
    receipt_id: ReceiptId, payload: OwnershipReviewPayload, response: Response
) -> ReceivingResponse:
    """귀속 확인 시도를 접수한다 (AC-08).

    자원 이름이 설계를 말해 준다. PATCH /receipts/{id}로 귀속을 갱신하는 모양이면
    확인 시도 이력이 남지 않아 AC-08이 요구하는 재확인·관리자 보고를 담을 수
    없다. 검토를 자원으로 두면 실패한 시도도 기록된다.
    """
    with unit_of_work() as session:
        result = review_ownership(
            session,
            OwnershipReviewCommand(
                receipt_id=receipt_id,
                outcome=payload.outcome,
                reviewed_by=payload.reviewed_by,
                occurred_at=payload.occurred_at,
                seller_code=payload.seller_code,
                sku_code=payload.sku_code,
                note=payload.note,
                due_at=payload.due_at,
            ),
        )

    response.status_code = _STATUS_BY_OUTCOME[result.outcome]
    return _to_response(result)

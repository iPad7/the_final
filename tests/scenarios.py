"""통합·동시성 테스트가 공유하는 업무 시나리오 조립기.

각 함수가 자기 트랜잭션을 열고 닫는다. 커밋된 상태 위에서 다음 판정이 일어나는
것이 실제 흐름이며, 한 트랜잭션에 몰아넣으면 잠금이 자기 자신과 경합하지 않아
검증력이 떨어진다.

SIMULATION 데이터만 쓴다(ADR23-08).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from fulfill.modules.allocation.domain import AllocationRunCommand
from fulfill.modules.inventory.domain import QC_FAIL, QC_PASS, QcCommand, ReceiptCommand
from fulfill.modules.inventory.queries import find_sku_ids_by_codes
from fulfill.modules.requests.domain import IntakeCommand, Recipient, RequestedLine
from fulfill.shared.db import unit_of_work
from fulfill.workflows.accept_request import IntakeOutcome, accept_request
from fulfill.workflows.allocate import (
    AllocationResult,
    RequestAllocationSummary,
    run_allocation,
    summarize_request_allocation,
)
from fulfill.workflows.receiving import ReceivingOutcome, record_qc_decision, record_receipt
from tests.seed_data import SELLER_A, SKU_P

NOW = datetime(2026, 10, 30, 9, tzinfo=UTC)
RECEIVED_AT = datetime(2026, 10, 30, 9, 30, tzinfo=UTC)
DUE = datetime(2026, 11, 5, tzinfo=UTC)
RECIPIENT = Recipient(name="홍길동", address="서울시 어딘가 101", phone="010-0000-0000")


def sku_id_of(session: Session, seller_code: str = SELLER_A, sku_code: str = SKU_P) -> uuid.UUID:
    """판매사 코드와 품목 코드로 SKU id를 찾는다."""
    seller_id = session.execute(
        text("SELECT id FROM seller WHERE code = :code"), {"code": seller_code}
    ).scalar_one()
    return find_sku_ids_by_codes(session, seller_id, [sku_code])[sku_code]


def place_request(
    external_id: str,
    quantity: int,
    sku_code: str = SKU_P,
    seller_code: str = SELLER_A,
) -> uuid.UUID:
    """출고 요청 하나를 접수한다. 라인은 하나뿐이다.

    호출 순서가 FIFO 순서가 된다. 그것이 테스트의 전제다.
    """
    with unit_of_work() as session:
        result = accept_request(
            session,
            IntakeCommand(
                seller_code=seller_code,
                external_request_id=external_id,
                lines=(RequestedLine(sku_code=sku_code, quantity=quantity),),
                recipient=RECIPIENT,
                initial_due_at=DUE,
                desired_ship_date=None,
                occurred_at=NOW,
            ),
            received_at=RECEIVED_AT,
        )
    assert result.outcome is IntakeOutcome.APPLIED
    assert result.request_id is not None
    return result.request_id


def receive_stock(quantity: int, sku_code: str = SKU_P, seller_code: str = SELLER_A) -> uuid.UUID:
    """실도착을 기록한다. 버킷은 미검수로 생긴다."""
    with unit_of_work() as session:
        result = record_receipt(
            session,
            ReceiptCommand(
                arrived_quantity=quantity,
                confirmed_by="WH-STAFF-01",
                arrived_at=NOW,
                seller_code=seller_code,
                sku_code=sku_code,
            ),
        )
    assert result.outcome is ReceivingOutcome.APPLIED
    assert result.receipt_id is not None
    return result.receipt_id


def judge_qc(receipt_id: uuid.UUID, result: str, quantity: int) -> None:
    """검수 판정을 기록한다. 불합격은 격리 구역으로 보낸다."""
    with unit_of_work() as session:
        outcome = record_qc_decision(
            session,
            QcCommand(
                receipt_id=receipt_id,
                result=result,
                quantity=quantity,
                decided_by="QC-STAFF-01",
                occurred_at=NOW,
                location_code="Q-01" if result == QC_FAIL else None,
            ),
        )
    assert outcome.outcome is ReceivingOutcome.APPLIED


def make_passed_stock(
    quantity: int, sku_code: str = SKU_P, seller_code: str = SELLER_A
) -> uuid.UUID:
    """적격 재고를 만든다. 실도착 전량을 QC 합격시킨다."""
    receipt_id = receive_stock(quantity, sku_code=sku_code, seller_code=seller_code)
    judge_qc(receipt_id, QC_PASS, quantity)
    return receipt_id


def run_fifo(sku_code: str = SKU_P, seller_code: str = SELLER_A) -> AllocationResult:
    """배정을 한 번 실행한다."""
    with unit_of_work() as session:
        return run_allocation(
            session, AllocationRunCommand(seller_code=seller_code, sku_code=sku_code)
        )


def allocation_summary_of(request_id: uuid.UUID) -> RequestAllocationSummary:
    """요청의 SKU별 배정 현황. 없는 요청이면 테스트가 바로 깨진다."""
    with unit_of_work() as session:
        summary = summarize_request_allocation(session, request_id)
    assert summary is not None
    return summary


def allocated_for(request_id: uuid.UUID) -> int:
    """요청 전체의 배정 수량. 라인이 하나인 테스트에서 쓴다."""
    return sum(line.allocated_quantity for line in allocation_summary_of(request_id).lines)

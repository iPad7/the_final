"""입고와 부분 QC를 실제 PostgreSQL에서 검증한다 (AC-06, AC-07, AC-08).

판정이 DB 제약과 맞물려 있어 SQLite로는 보일 수 없다. 버킷 유니크 제약의
NULLS NOT DISTINCT, 복합 FK의 MATCH SIMPLE, 수량 음수 금지가 모두 검증 대상이다.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from fulfill.modules.inventory.domain import (
    OWNERSHIP_CONFIRMED,
    OWNERSHIP_UNKNOWN,
    QC_FAIL,
    QC_PASS,
    QC_STATE_FAILED,
    QC_STATE_PASSED,
    QC_STATE_PENDING,
    REVIEW_CONFIRMED,
    REVIEW_ESCALATED,
    REVIEW_STILL_UNKNOWN,
    AdviceCommand,
    OwnershipReviewCommand,
    QcCommand,
    ReceiptCommand,
)
from fulfill.modules.inventory.queries import (
    find_allocatable_quantity,
    find_sku_ids_by_codes,
    summarize_advice,
    summarize_receipt,
)
from fulfill.shared.db import unit_of_work
from fulfill.workflows.receiving import (
    ReceivingOutcome,
    record_inbound_advice,
    record_qc_decision,
    record_receipt,
    review_ownership,
)
from tests.seed_data import SELLER_A, SELLER_B, SKU_P

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 30, 9, tzinfo=UTC)
DUE = datetime(2026, 11, 6, 9, tzinfo=UTC)


def _sku_id(session: Session, seller_code: str, sku_code: str) -> uuid.UUID:
    seller_id = session.execute(
        text("SELECT id FROM seller WHERE code = :code"), {"code": seller_code}
    ).scalar_one()
    return find_sku_ids_by_codes(session, seller_id, [sku_code])[sku_code]


def _advise(expected_quantity: int) -> uuid.UUID:
    """판매사 A의 SKU_P에 입고 예정을 통보한다."""
    with unit_of_work() as session:
        result = record_inbound_advice(
            session,
            AdviceCommand(
                seller_code=SELLER_A,
                sku_code=SKU_P,
                expected_quantity=expected_quantity,
                advised_at=NOW,
            ),
        )
    assert result.outcome is ReceivingOutcome.APPLIED
    assert result.advice_id is not None
    return result.advice_id


def _receive(
    quantity: int, advice_id: uuid.UUID | None = None, attributed: bool = True
) -> uuid.UUID:
    """실도착을 기록한다. attributed=False면 소유자 미확인 입고다."""
    with unit_of_work() as session:
        result = record_receipt(
            session,
            ReceiptCommand(
                arrived_quantity=quantity,
                confirmed_by="WH-STAFF-01",
                arrived_at=NOW,
                seller_code=SELLER_A if attributed else None,
                sku_code=SKU_P if attributed else None,
                advice_id=advice_id,
            ),
        )
    assert result.outcome is ReceivingOutcome.APPLIED, result.problems
    assert result.receipt_id is not None
    return result.receipt_id


def _decide(receipt_id: uuid.UUID, result_value: str, quantity: int, location: str | None = None):
    with unit_of_work() as session:
        return record_qc_decision(
            session,
            QcCommand(
                receipt_id=receipt_id,
                result=result_value,
                quantity=quantity,
                decided_by="QC-STAFF-01",
                occurred_at=NOW,
                location_code=location,
            ),
        )


def test_예정량은_재고가_되지_않는다(seeded_database: str) -> None:
    """AC-06: 예정 600개를 물리 정상 재고나 검수 합격으로 취급하지 않는다."""
    advice_id = _advise(600)

    with unit_of_work() as session:
        summary = summarize_advice(session, advice_id)
        assert summary is not None
        assert (summary.expected_quantity, summary.arrived_quantity) == (600, 0)
        # 예정 통보만으로는 배정 가능 수량이 0이다.
        assert find_allocatable_quantity(session, _sku_id(session, SELLER_A, SKU_P)) == 0


def test_실도착과_예정_대비_부족을_구별한다(seeded_database: str) -> None:
    """AC-06: 실도착 590과 부족 10을 따로 확인할 수 있다."""
    advice_id = _advise(600)
    _receive(590, advice_id=advice_id)

    with unit_of_work() as session:
        summary = summarize_advice(session, advice_id)
        assert summary is not None
        assert summary.expected_quantity == 600
        assert summary.arrived_quantity == 590
        assert summary.shortfall == 10


def test_분할_입고를_합산한다(seeded_database: str) -> None:
    """AC-06: 한 예정에 여러 실도착이 붙는 것은 정상 업무다."""
    advice_id = _advise(600)
    _receive(400, advice_id=advice_id)
    _receive(190, advice_id=advice_id)

    with unit_of_work() as session:
        summary = summarize_advice(session, advice_id)
        assert summary is not None
        assert (summary.arrived_quantity, summary.shortfall) == (590, 10)


def test_예정보다_많이_도착하면_부족분은_0이다(seeded_database: str) -> None:
    advice_id = _advise(600)
    _receive(610, advice_id=advice_id)

    with unit_of_work() as session:
        summary = summarize_advice(session, advice_id)
        assert summary is not None
        assert summary.shortfall == 0


def test_실도착은_미검수_버킷으로_들어간다(seeded_database: str) -> None:
    """AC-06: 실제 도착 수량 기준으로 검수를 이어간다."""
    receipt_id = _receive(590)

    with unit_of_work() as session:
        summary = summarize_receipt(session, receipt_id)
        assert summary is not None
        assert summary.arrived_quantity == 590
        assert summary.quantity_in(QC_STATE_PENDING) == 590
        assert summary.quantity_in(QC_STATE_PASSED) == 0
        # 아직 검수하지 않았으므로 배정 자격이 없다.
        assert find_allocatable_quantity(session, _sku_id(session, SELLER_A, SKU_P)) == 0


def test_부분_합격분만_배정_대상이_된다(seeded_database: str) -> None:
    """AC-07: 합격 570만 배정 대상, 불합격 20은 격리, 수량은 보존된다."""
    receipt_id = _receive(590)

    # 합격을 두 번 나눠 판정한다. 두 번째가 같은 버킷에 더해지는 경로다.
    assert _decide(receipt_id, QC_PASS, 300).outcome is ReceivingOutcome.APPLIED
    assert _decide(receipt_id, QC_PASS, 270).outcome is ReceivingOutcome.APPLIED
    assert _decide(receipt_id, QC_FAIL, 20, location="Q-01").outcome is ReceivingOutcome.APPLIED

    with unit_of_work() as session:
        summary = summarize_receipt(session, receipt_id)
        assert summary is not None
        assert summary.quantity_in(QC_STATE_PASSED) == 570
        assert summary.quantity_in(QC_STATE_FAILED) == 20
        assert summary.quantity_in(QC_STATE_PENDING) == 0
        # 수량 보존: 판정은 수량을 만들지도 없애지도 않는다.
        assert summary.bucket_total == summary.arrived_quantity == 590

        # 합격 버킷이 한 행으로 합쳐졌다. NULLS NOT DISTINCT가 없으면 두 행이 된다.
        passed = [b for b in summary.buckets if b.qc_state == QC_STATE_PASSED]
        assert len(passed) == 1

        # 불합격품은 격리 구역에 있다 (AC-07의 업무 근거).
        failed = [b for b in summary.buckets if b.qc_state == QC_STATE_FAILED]
        assert [b.location_code for b in failed] == ["Q-01"]

        # 배정 자격은 합격분뿐이다. 미검수·불합격이 섞이면 이 값이 590이 된다.
        assert find_allocatable_quantity(session, _sku_id(session, SELLER_A, SKU_P)) == 570


def test_미검수_잔량을_넘는_판정을_거절한다(seeded_database: str) -> None:
    """AC-07: 과검수는 업무 사유로 거절한다."""
    receipt_id = _receive(590)
    assert _decide(receipt_id, QC_PASS, 570).outcome is ReceivingOutcome.APPLIED

    rejected = _decide(receipt_id, QC_FAIL, 30, location="Q-01")
    assert rejected.outcome is ReceivingOutcome.REJECTED
    assert {p.field for p in rejected.problems} == {"quantity"}

    with unit_of_work() as session:
        summary = summarize_receipt(session, receipt_id)
        assert summary is not None
        # 거절된 판정이 아무것도 바꾸지 않았다.
        assert summary.bucket_total == 590
        assert summary.quantity_in(QC_STATE_PENDING) == 20


def test_위치가_미확정이어도_배정_자격은_있다(seeded_database: str) -> None:
    """NBR-12, AC-09: QC 합격 후 위치 미확정이어도 배정은 가능하다."""
    receipt_id = _receive(100)
    _decide(receipt_id, QC_PASS, 100)

    with unit_of_work() as session:
        summary = summarize_receipt(session, receipt_id)
        assert summary is not None
        passed = [b for b in summary.buckets if b.qc_state == QC_STATE_PASSED]
        # 위치는 아직 NULL이다.
        assert [b.location_code for b in passed] == [None]
        # 그래도 배정 자격은 있다. 여기서 걸러 버리면 AC-09를 위반한다.
        assert find_allocatable_quantity(session, _sku_id(session, SELLER_A, SKU_P)) == 100


def _review(receipt_id: uuid.UUID, outcome: str, **kw: object):
    with unit_of_work() as session:
        return review_ownership(
            session,
            OwnershipReviewCommand(
                receipt_id=receipt_id,
                outcome=outcome,
                reviewed_by="WH-MGR-01",
                occurred_at=NOW,
                **kw,  # type: ignore[arg-type]
            ),
        )


def _review_count(session: Session, receipt_id: uuid.UUID) -> int:
    return int(
        session.execute(
            text("SELECT count(*) FROM ownership_review WHERE receipt_id = :r"),
            {"r": receipt_id},
        ).scalar_one()
    )


def test_소유자_미확인_입고는_배정_대상이_아니다(seeded_database: str) -> None:
    """AC-08: 미확인품은 별도 보관하고 정상 재고 배정을 막는다."""
    receipt_id = _receive(80, attributed=False)

    with unit_of_work() as session:
        summary = summarize_receipt(session, receipt_id)
        assert summary is not None
        assert summary.ownership_status == OWNERSHIP_UNKNOWN
        assert summary.arrived_quantity == 80
        # 버킷이 아예 없다. 배정 조회가 볼 수 있는 곳에 수량이 존재하지 않는다.
        assert summary.buckets == ()
        assert find_allocatable_quantity(session, _sku_id(session, SELLER_A, SKU_P)) == 0


def test_미확인_입고는_검수할_수_없다(seeded_database: str) -> None:
    """AC-08: 귀속 확인 전에는 QC로 정상 재고에 편입할 수 없다."""
    receipt_id = _receive(80, attributed=False)

    rejected = _decide(receipt_id, QC_PASS, 80)
    assert rejected.outcome is ReceivingOutcome.REJECTED
    assert {p.field for p in rejected.problems} == {"quantity"}


def test_확인하지_못한_검토는_이력만_쌓는다(seeded_database: str) -> None:
    """AC-08, AC-11: 기한 초과를 자동 폐기·승인으로 해석하지 않는다."""
    receipt_id = _receive(80, attributed=False)

    assert (
        _review(
            receipt_id, REVIEW_STILL_UNKNOWN, note="라벨 훼손으로 식별 불가", due_at=DUE
        ).outcome
        is ReceivingOutcome.APPLIED
    )
    assert (
        _review(
            receipt_id, REVIEW_ESCALATED, note="공통 확인 기한 초과, 관리자 판단 요청", due_at=DUE
        ).outcome
        is ReceivingOutcome.APPLIED
    )

    with unit_of_work() as session:
        summary = summarize_receipt(session, receipt_id)
        assert summary is not None
        # 상태는 그대로이고 재고도 생기지 않았다.
        assert summary.ownership_status == OWNERSHIP_UNKNOWN
        assert summary.buckets == ()
        # 두 번의 시도가 모두 기록으로 남았다. 이것이 AC-08의 업무 근거다.
        assert _review_count(session, receipt_id) == 2


def test_귀속_확인_후에도_검수가_따로_필요하다(seeded_database: str) -> None:
    """AC-08, NBR-16: 소유권이 확인되어도 별도 QC가 필요하다."""
    receipt_id = _receive(80, attributed=False)
    _review(receipt_id, REVIEW_STILL_UNKNOWN, note="식별 불가", due_at=DUE)

    assert (
        _review(receipt_id, REVIEW_CONFIRMED, seller_code=SELLER_A, sku_code=SKU_P).outcome
        is ReceivingOutcome.APPLIED
    )

    with unit_of_work() as session:
        summary = summarize_receipt(session, receipt_id)
        assert summary is not None
        assert summary.ownership_status == OWNERSHIP_CONFIRMED
        # 버킷이 생겼지만 미검수다. 확인이 QC를 대신하지 않는다.
        assert summary.quantity_in(QC_STATE_PENDING) == 80
        assert summary.quantity_in(QC_STATE_PASSED) == 0
        assert summary.bucket_total == summary.arrived_quantity == 80
        # 아직 배정 자격은 없다.
        assert find_allocatable_quantity(session, _sku_id(session, SELLER_A, SKU_P)) == 0
        assert _review_count(session, receipt_id) == 2

    # 확인된 뒤에는 검수할 수 있다.
    assert _decide(receipt_id, QC_PASS, 80).outcome is ReceivingOutcome.APPLIED
    with unit_of_work() as session:
        assert find_allocatable_quantity(session, _sku_id(session, SELLER_A, SKU_P)) == 80


def test_이미_확인된_입고는_다시_검토할_수_없다(seeded_database: str) -> None:
    """AC-18, AC-36: 확인된 귀속을 되돌리는 것은 별도 권한의 기록 정정이다."""
    receipt_id = _receive(80, attributed=False)
    _review(receipt_id, REVIEW_CONFIRMED, seller_code=SELLER_A, sku_code=SKU_P)

    rejected = _review(receipt_id, REVIEW_CONFIRMED, seller_code=SELLER_A, sku_code=SKU_P)
    assert rejected.outcome is ReceivingOutcome.REJECTED
    assert {p.field for p in rejected.problems} == {"receipt_id"}

    with unit_of_work() as session:
        summary = summarize_receipt(session, receipt_id)
        assert summary is not None
        # 거절된 검토는 이력도 남기지 않고 수량도 늘리지 않았다.
        assert _review_count(session, receipt_id) == 1
        assert summary.bucket_total == 80


def test_다른_판매사의_재고와_섞이지_않는다(seeded_database: str) -> None:
    """AC-07: 판매사별로 배정 자격을 따로 계산한다."""
    receipt_id = _receive(590)
    _decide(receipt_id, QC_PASS, 590)

    with unit_of_work() as session:
        assert find_allocatable_quantity(session, _sku_id(session, SELLER_A, SKU_P)) == 590
        # 판매사 B의 같은 코드 SKU는 다른 물건이다.
        assert find_allocatable_quantity(session, _sku_id(session, SELLER_B, SKU_P)) == 0


# 아래 세 테스트는 애플리케이션을 우회해 직접 INSERT한다. 조정 계층을 통하면
# 도메인이 먼저 막아서, DB 제약이 실제로 동작하는지는 알 수 없다. 최종 방어선은
# 애플리케이션 버그가 있어도 성립해야 하므로 따로 확인한다.

INSERT_RECEIPT = text(
    "INSERT INTO receipt"
    " (id, seller_id, sku_id, ownership_status, arrived_quantity,"
    "  confirmed_by, environment_class, arrived_at)"
    " VALUES (gen_random_uuid(), :seller_id, :sku_id, :status, 10,"
    "         'WH-STAFF-01', 'SIMULATION', now())"
)


def test_다른_판매사의_품목을_귀속시킬_수_없다(seeded_database: str) -> None:
    """복합 FK가 재고 섞임을 막는다. MATCH SIMPLE이라 부분 귀속은 통과한다."""
    with unit_of_work() as session:
        seller_a = session.execute(
            text("SELECT id FROM seller WHERE code = :c"), {"c": SELLER_A}
        ).scalar_one()
        sku_of_b = _sku_id(session, SELLER_B, SKU_P)

    with pytest.raises(Exception, match="fk_receipt_seller_sku"), unit_of_work() as session:
        session.execute(
            INSERT_RECEIPT,
            {"seller_id": seller_a, "sku_id": sku_of_b, "status": OWNERSHIP_CONFIRMED},
        )


def test_귀속_없이_확인됐다고_기록할_수_없다(seeded_database: str) -> None:
    """CONFIRMED는 귀속을 지목했다는 주장이며 DB가 그것을 강제한다."""
    with (
        pytest.raises(Exception, match="ck_receipt_confirmed_attribution"),
        unit_of_work() as session,
    ):
        session.execute(
            INSERT_RECEIPT,
            {"seller_id": None, "sku_id": None, "status": OWNERSHIP_CONFIRMED},
        )


def test_위치_미확정_버킷은_중복될_수_없다(seeded_database: str) -> None:
    """uq_stock_bucket의 NULLS NOT DISTINCT. 없으면 위치 NULL 버킷이 쌓인다."""
    receipt_id = _receive(590)

    with pytest.raises(Exception, match="uq_stock_bucket"), unit_of_work() as session:
        sku_id = _sku_id(session, SELLER_A, SKU_P)
        session.execute(
            text(
                "INSERT INTO stock_position"
                " (id, sku_id, receipt_id, qc_state, location_code, quantity)"
                " VALUES (gen_random_uuid(), :sku_id, :receipt_id, :state, NULL, 1)"
            ),
            {"sku_id": sku_id, "receipt_id": receipt_id, "state": QC_STATE_PENDING},
        )

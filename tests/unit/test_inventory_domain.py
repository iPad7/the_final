"""입고·검수 도메인 규칙 (AC-06, AC-07, AC-08).

DB 없이 돌아간다. 도메인이 SQLAlchemy를 모르는 덕분이며, import 계약이 그것을
강제한다.
"""

import uuid
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime

from fulfill.modules.inventory.domain import (
    OWNERSHIP_CONFIRMED,
    OWNERSHIP_UNKNOWN,
    QC_FAIL,
    QC_PASS,
    REVIEW_CONFIRMED,
    REVIEW_ESCALATED,
    REVIEW_STILL_UNKNOWN,
    OwnershipReviewCommand,
    QcCommand,
    ReceiptCommand,
    ValidationProblem,
    validate_ownership_review,
    validate_qc_decision,
    validate_receipt,
)

NOW = datetime(2026, 10, 30, 9, tzinfo=UTC)
NAIVE = datetime(2026, 10, 30, 9)
RECEIPT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

# 실도착 590. AC-06의 예정 600에 대한 도착량이다.
ARRIVED = ReceiptCommand(
    arrived_quantity=590,
    confirmed_by="WH-STAFF-01",
    arrived_at=NOW,
    seller_code="SELLER-A",
    sku_code="SKU-P",
)


def _fields(problems: Iterable[ValidationProblem]) -> set[str]:
    """거절 사유의 필드 이름만 모은다. 문구가 바뀌어도 테스트가 버티게 한다."""
    return {problem.field for problem in problems}


def test_예정보다_적게_도착해도_유효하다() -> None:
    # AC-06이 정상 경로로 처리하라고 요구하는 바로 그 시나리오다. 여기서 거절하면
    # 예정 600에 590이 온 업무를 기록할 방법이 없어진다.
    assert validate_receipt(ARRIVED) == ()


def test_예정보다_많이_도착해도_유효하다() -> None:
    # 실물이 왔다는 사실은 기록해야 한다. 차이는 조회가 보여줄 값이지 접수를 막을
    # 근거가 아니다.
    assert validate_receipt(replace(ARRIVED, arrived_quantity=610)) == ()


def test_귀속을_모르는_입고도_유효하다() -> None:
    # AC-08의 출발점. 소유자를 모르는 물건도 도착한 것은 사실이다.
    unattributed = replace(ARRIVED, seller_code=None, sku_code=None)
    assert validate_receipt(unattributed) == ()


def test_판매사와_품목은_함께_지정해야_한다() -> None:
    # SKU가 판매사에 종속되므로 품목만 아는 상태는 성립하지 않는다. DB도 같은
    # 규칙을 ck_receipt_sku_implies_seller로 막는다.
    assert _fields(validate_receipt(replace(ARRIVED, seller_code=None))) == {"sku_code"}
    assert _fields(validate_receipt(replace(ARRIVED, sku_code=None))) == {"sku_code"}


def test_실도착_수량은_양수여야_한다() -> None:
    assert _fields(validate_receipt(replace(ARRIVED, arrived_quantity=0))) == {"arrived_quantity"}


def test_입고_담당자가_필요하다() -> None:
    # AC-06의 업무 근거에 "입고 담당 확인"이 있다. 누가 셌는지 모르면 근거가 아니다.
    assert _fields(validate_receipt(replace(ARRIVED, confirmed_by="   "))) == {"confirmed_by"}


def test_도착_시각에_시간대가_있어야_한다() -> None:
    # 시간대 없는 시점은 모호하고, 사건이 지난 뒤에는 복구할 수 없다.
    assert _fields(validate_receipt(replace(ARRIVED, arrived_at=NAIVE))) == {"arrived_at"}


# 합격 570 판정. AC-07의 실도착 590 중 570 합격 시나리오다.
PASSED = QcCommand(
    receipt_id=RECEIPT_ID,
    result=QC_PASS,
    quantity=570,
    decided_by="QC-STAFF-01",
    occurred_at=NOW,
)


def test_미검수_잔량_안의_합격_판정은_유효하다() -> None:
    assert validate_qc_decision(PASSED, pending_quantity=590) == ()


def test_미검수가_남아_있어도_부분_합격은_유효하다() -> None:
    # AC-07의 위반 조항은 "나머지 검수가 끝나지 않았다는 이유로 570개 배정을 일괄
    # 금지"하는 것이다. 570을 합격시킬 수 있어야 그 금지가 일어나지 않는다.
    assert validate_qc_decision(PASSED, pending_quantity=590) == ()
    remaining = replace(PASSED, result=QC_FAIL, quantity=20, location_code="Q-01")
    assert validate_qc_decision(remaining, pending_quantity=20) == ()


def test_미검수_잔량을_넘는_판정은_거절한다() -> None:
    # 과검수. stock_position의 quantity >= 0이 DB에서도 막지만, 거기까지 가면
    # 담당자가 업무 사유 대신 서버 오류를 받는다.
    problems = validate_qc_decision(PASSED, pending_quantity=20)
    assert _fields(problems) == {"quantity"}
    assert "20" in problems[0].reason


def test_미검수가_0이면_모든_판정을_거절한다() -> None:
    # 귀속 미확인 입고가 이 상태다. 버킷이 없으므로 잔량이 0이고, AC-08의
    # "확인 후에도 QC 없이 편입 금지"가 이 형태로 나타난다.
    assert _fields(validate_qc_decision(PASSED, pending_quantity=0)) == {"quantity"}


def test_합격_불합격_외의_판정값은_거절한다() -> None:
    # 미검수를 뜻하는 판정값은 없다. 판정의 부재는 판정이 아니다.
    assert _fields(validate_qc_decision(replace(PASSED, result="PENDING"), 590)) == {"result"}


def test_판정_수량은_양수여야_한다() -> None:
    assert _fields(validate_qc_decision(replace(PASSED, quantity=0), 590)) == {"quantity"}


def test_검수_담당자가_필요하다() -> None:
    # AC-07의 업무 근거에 "검수 주체"가 있다.
    assert _fields(validate_qc_decision(replace(PASSED, decided_by=" "), 590)) == {"decided_by"}


def test_판정_시각에_시간대가_있어야_한다() -> None:
    # AC-07의 업무 근거에 "판정 시각"이 있다.
    assert _fields(validate_qc_decision(replace(PASSED, occurred_at=NAIVE), 590)) == {"occurred_at"}


# 귀속을 확인한 검토. AC-08의 미식별 상품 80개가 확인되는 시나리오다.
CONFIRMED = OwnershipReviewCommand(
    receipt_id=RECEIPT_ID,
    outcome=REVIEW_CONFIRMED,
    reviewed_by="WH-MGR-01",
    occurred_at=NOW,
    seller_code="SELLER-A",
    sku_code="SKU-P",
)

# 확인하지 못한 검토. 사유가 있어야 업무 근거가 된다.
UNRESOLVED = OwnershipReviewCommand(
    receipt_id=RECEIPT_ID,
    outcome=REVIEW_STILL_UNKNOWN,
    reviewed_by="WH-MGR-01",
    occurred_at=NOW,
    note="라벨 훼손으로 판매사 식별 불가",
    due_at=datetime(2026, 11, 6, 9, tzinfo=UTC),
)


def test_소유자를_지목한_확인은_유효하다() -> None:
    assert validate_ownership_review(CONFIRMED, OWNERSHIP_UNKNOWN) == ()


def test_확인에는_판매사와_품목이_모두_필요하다() -> None:
    assert _fields(
        validate_ownership_review(replace(CONFIRMED, sku_code=None), OWNERSHIP_UNKNOWN)
    ) == {"sku_code"}
    assert _fields(
        validate_ownership_review(replace(CONFIRMED, seller_code=None), OWNERSHIP_UNKNOWN)
    ) == {"seller_code"}


def test_사유가_있는_미확인_검토는_유효하다() -> None:
    # 확인하지 못한 것도 기록해야 하는 사실이다. AC-08은 "확인·재확인·관리자
    # 보고를 기록"하라고 요구한다.
    assert validate_ownership_review(UNRESOLVED, OWNERSHIP_UNKNOWN) == ()


def test_기한_초과_에스컬레이션도_유효하다() -> None:
    # NBR-17·AC-11은 기한 초과를 자동 폐기나 승인으로 해석하지 말고 재확인·관리자
    # 보고를 기록하라고 요구한다.
    escalated = replace(UNRESOLVED, outcome=REVIEW_ESCALATED, note="공통 확인 기한 초과")
    assert validate_ownership_review(escalated, OWNERSHIP_UNKNOWN) == ()


def test_미확인_검토에는_사유가_필요하다() -> None:
    # 사유 없는 "확인 못 함"은 기록이 아니라 빈칸이다.
    assert _fields(
        validate_ownership_review(replace(UNRESOLVED, note=None), OWNERSHIP_UNKNOWN)
    ) == {"note"}


def test_미확인_검토에_소유자를_적을_수_없다() -> None:
    # AC-08의 위반 조항은 "소유자를 모르는 상태에서 특정 판매사 계약 기한을 임의
    # 적용"하는 것이다. 이 입력을 받아 주면 그 경로가 열린다.
    contradictory = replace(UNRESOLVED, seller_code="SELLER-A")
    assert _fields(validate_ownership_review(contradictory, OWNERSHIP_UNKNOWN)) == {"outcome"}


def test_이미_확인된_입고는_다시_검토할_수_없다() -> None:
    # 확인된 귀속을 되돌리는 것은 기록 정정(AC-18)이고 별도 권한이다. 조용히
    # 허용하면 AC-36의 이력 보존을 우회하는 경로가 생긴다.
    assert _fields(validate_ownership_review(CONFIRMED, OWNERSHIP_CONFIRMED)) == {"receipt_id"}


def test_허용되지_않는_검토_결과값은_거절한다() -> None:
    assert "outcome" in _fields(
        validate_ownership_review(replace(CONFIRMED, outcome="MAYBE"), OWNERSHIP_UNKNOWN)
    )


def test_검토_담당자가_필요하다() -> None:
    assert _fields(
        validate_ownership_review(replace(CONFIRMED, reviewed_by=""), OWNERSHIP_UNKNOWN)
    ) == {"reviewed_by"}


def test_확인_기한에_시간대가_있어야_한다() -> None:
    # 기한은 AC-08의 업무 근거이고, 시간대가 없으면 초과 여부를 판정할 수 없다.
    assert _fields(
        validate_ownership_review(replace(UNRESOLVED, due_at=NAIVE), OWNERSHIP_UNKNOWN)
    ) == {"due_at"}

"""FIFO 배정의 순수 도메인 로직 (BC-04).

이 모듈은 FastAPI도 SQLAlchemy도 import하지 않는다(ADR23 §3). 배분 계산을 I/O와
떼어 두는 데는 분명한 이유가 있다. ADR23 §4가 "단순한 read-check-write만으로
정합성을 보장하지 않으며, 락을 잡아도 도메인 FIFO 보장은 자동으로 생성되지
않는다"고 경고했다. 잠금은 동시에 끼어드는 것을 막을 뿐이고, 순서를 지키는 것은
별개의 계산이다. 그 계산을 DB 없이 전부 시험할 수 있어야 한다.

아래 업무 어휘를 `models.py`가 가져다 쓴다. 의존 방향이 models → domain이다.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from fulfill.shared.validation import ValidationProblem, normalize_text


@dataclass(frozen=True)
class DemandSlot:
    """배정을 기다리는 수요 구간 하나.

    `outstanding`은 구간의 총 수량이 아니라 **아직 배정되지 않은** 수량이다. 이
    구분이 중요하다. 총 수량을 넘겨주면 이미 배정된 몫을 또 배정하게 된다.

    DemandSegment에 잔량 컬럼을 두지 않았으므로(BC-02의 결정) 이 값은 조정
    계층이 수요 수량에서 유효 배정량을 빼서 계산해 넘긴다.
    """

    segment_id: uuid.UUID
    # FIFO 순서의 유일한 근거다. 생성 시각이나 요청 id가 아니다(20 §4:
    # "단순 DB 생성시각으로 FIFO를 대체하지 않는다").
    seq: int
    outstanding: int


@dataclass(frozen=True)
class SupplySlot:
    """배정에 쓸 수 있는 재고 버킷 하나.

    `available`은 버킷 수량이 아니라 **아직 배정되지 않은** 수량이다. 이미 다른
    요청에 배정된 몫을 빼고 넘긴다. NBR-04의 "한 번 배정된 재고는 중복 배정하지
    않는다"가 이 뺄셈에서 시작한다.

    QC 상태와 귀속은 여기 없다. 호출자가 적격 버킷만 골라 넘기기 때문이다. 이
    계산에 "합격인가"를 다시 묻는 조건을 넣으면 판정 기준이 두 곳에 생긴다.
    """

    bucket_id: uuid.UUID
    available: int


@dataclass(frozen=True)
class PlannedAllocation:
    """수요 구간 하나에 버킷 하나에서 배정할 수량.

    버킷을 가리키는 것이 설계의 선택이다. 수요 구간만 가리키고 수량만 적으면
    "어느 실물인가"를 잃는다. 그러면 24-4가 AC-09의 피킹 위치를 확인할 대상을
    특정할 수 없고, AC-15의 대체 피킹도 어느 물건이 손상됐는지 연결할 수 없다.

    한 수요 구간이 여러 버킷에서 채워지는 것은 정상이다. 입고가 분할되면 적격
    수량이 여러 버킷에 흩어지므로, 수요 5개가 버킷 3+2로 채워진다.
    """

    segment_id: uuid.UUID
    bucket_id: uuid.UUID
    quantity: int


@dataclass(frozen=True)
class AllocationRunCommand:
    """이 판매사·품목의 미충족 수요에 적격 재고를 배정하라는 명령.

    담당자를 받지 않는다. 입고·검수와 다른 점이고 의도한 차이다. 실도착 590개는
    누군가 세어 확인한 사실이라 `confirmed_by`가 근거였지만, 배정은 FIFO 규칙에서
    기계적으로 도출되는 결정이다. 누가 실행 버튼을 눌렀는지는 결과를 바꾸지
    않으므로 업무 근거가 아니다.

    해제는 다르다. "왜 풀었는가"가 정당성의 근거이므로 단계 2의 해제 명령은
    사유를 요구한다(CB-03의 "정당 해제된 물량만").

    시각도 받지 않는다. 배정 시각은 3PL이 관찰하는 사실이고 클라이언트가 주장할
    수 없다. 24-1의 `received_at`과 같은 이유로 서버가 정한다.
    """

    seller_code: str
    sku_code: str


def validate_allocation_run(command: AllocationRunCommand) -> tuple[ValidationProblem, ...]:
    """배정 실행 명령의 유효성을 판정한다 (AC-03, AC-04).

    검사할 것이 코드 두 개뿐이다. 수량도 기한도 담당자도 없으므로 거절될 여지가
    거의 없고, 그것이 정상이다. 이 명령은 "무엇을 해 달라"가 아니라 "어디를
    다시 평가해 달라"는 요청이다.

    재고가 없는 경우를 거절로 만들지 않는다. 적격 수량 0은 정상 판정이며
    (NBR-01), 조정 계층이 그것을 UNCHANGED로 답한다.
    """
    problems: list[ValidationProblem] = []

    if not normalize_text(command.seller_code):
        problems.append(ValidationProblem("seller_code", "판매사 코드가 비어 있습니다"))
    if not normalize_text(command.sku_code):
        problems.append(ValidationProblem("sku_code", "SKU 코드가 비어 있습니다"))

    return tuple(problems)


def plan_allocations(
    demands: Iterable[DemandSlot], supplies: Iterable[SupplySlot]
) -> tuple[PlannedAllocation, ...]:
    """FIFO 순서로 적격 재고를 미충족 수요에 배분한다 (AC-03, AC-04).

    규칙이 전부 순차 소진에서 나온다. 수요를 `seq` 순으로 세우고 재고를 앞에서부터
    덜어 주면,

    - **선순위를 건너뛰지 않는다**(NBR-03): 선순위가 다 못 채웠다면 재고가 이미
      0이므로 후순위에 줄 것이 없다. 이 금지를 막는 조건문이 따로 없고, 애초에
      일어날 수 없다. 조건문으로 썼다면 빠뜨릴 수 있었다.
    - **부분 배정은 허용된다**(NBR-07): 수요 5개에 재고 3개면 3개를 준 뒤 멈춘다.
      0개를 주고 거절하지 않는다.
    - **구간 필요량을 넘지 않는다**: 각 구간은 `outstanding`까지만 받는다.
    - **이중 배정이 없다**: 버킷의 남은 가용량을 그 자리에서 깎으므로 같은 수량이
      두 구간에 들어가지 않는다.
    - **긴급성은 순서를 바꾸지 않는다**(NBR-08, AC-13): 이 계산에 긴급 여부가
      들어올 자리가 없다.

    수요는 여기서 정렬한다. 호출자가 정렬해 주기를 기대하지 않는다. FIFO 순서는
    도메인 규칙이고, 그 규칙을 지키는 책임이 조회문의 ORDER BY에 흩어지면 조회를
    바꾸는 사람이 규칙을 깨게 된다.

    재고는 정렬하지 않고 받은 순서대로 쓴다. 어느 실물을 어느 요청에 줄지는 어떤
    문서도 정하지 않았다. 호출자가 결정론적인 순서로만 넘겨 주면 된다.
    """
    # 가용량이 0인 버킷은 미리 버린다. 남겨 두면 루프가 빈 버킷을 훑는다.
    buckets = [slot for slot in supplies if slot.available > 0]
    # 버킷별 남은 가용량. 같은 길이의 별도 리스트로 두는 이유는 SupplySlot이
    # frozen이기 때문이다. 배분 중의 잔량은 입력 사실이 아니라 계산의 중간값이므로
    # 값 객체를 갱신하지 않는다.
    left = [slot.available for slot in buckets]
    plan: list[PlannedAllocation] = []
    cursor = 0

    for demand in sorted(demands, key=lambda slot: slot.seq):
        # 이미 전량 배정된 구간은 지나간다. 음수가 들어와도 같은 취급이다.
        need = demand.outstanding
        if need <= 0:
            continue

        while need > 0 and cursor < len(buckets):
            taken = min(need, left[cursor])
            plan.append(
                PlannedAllocation(
                    segment_id=demand.segment_id,
                    bucket_id=buckets[cursor].bucket_id,
                    quantity=taken,
                )
            )
            left[cursor] -= taken
            need -= taken
            # 버킷을 비웠을 때만 다음으로 넘어간다. 남아 있으면 다음 구간이
            # 이어서 쓴다.
            if left[cursor] == 0:
                cursor += 1

        if need > 0:
            # 재고가 떨어졌다. 뒤 구간을 건너뛰는 것이 아니라 줄 것이 없다.
            break

    return tuple(plan)

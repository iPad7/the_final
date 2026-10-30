"""여러 모듈이 함께 쓰는 입력 검증 요소.

업무 지식을 담지 않는다. 어느 업무 모듈에도 의존하지 않는 것이 shared에 둘 수
있는 조건이다(CLAUDE.md 6장). 그래서 domain 계층이 이것을 가져다 써도 순수성이
깨지지 않는다.
"""

import re
from dataclasses import dataclass

_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    """앞뒤 공백을 제거하고 연속 공백을 하나로 합친다.

    "A  101"과 "A 101"은 같은 주소이므로 합치는 것이 맞다. 다만 공백을 전부
    제거하면 서로 다른 주소가 합쳐질 수 있어 거기까지는 가지 않는다.

    재고 쪽에서는 이것이 편의가 아니라 정합성 문제다. stock_position의 유니크
    키에 location_code가 들어가므로, "A-01"과 "A-01 "을 서로 다른 값으로 두면
    같은 버킷이 두 행으로 쪼개지고 입고 수량 보존이 깨진다.
    """
    return _WHITESPACE_RUN.sub(" ", value).strip()


@dataclass(frozen=True)
class ValidationProblem:
    """거절 사유 하나.

    field는 입력 경로, reason은 판매사·담당자가 읽을 설명이다. 예외로 던지지
    않고 값으로 모으는 이유는 거절이 예상된 업무 결과이기 때문이다.
    """

    field: str
    reason: str

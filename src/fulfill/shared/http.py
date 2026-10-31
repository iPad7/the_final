"""HTTP 입구들이 함께 쓰는 표기 변환.

`shared/validation.py`와 나눠 둔 이유가 있다. 그쪽은 domain 계층이 import하므로
직렬화 라이브러리에 의존해서는 안 된다. 이 모듈은 api 계층만 쓴다.
"""

from pydantic.alias_generators import to_camel


def to_camel_path(path: str) -> str:
    """도메인 필드 경로를 JSON 표기로 바꾼다.

    요청을 camelCase로 받았으므로 거절 사유도 같은 표기여야 보낸 쪽이 자기가 보낸
    필드를 찾을 수 있다. 도메인은 HTTP 표기를 알지 못하므로 변환은 이 계층에서
    한다.

    DTO의 alias_generator와 같은 함수를 쓴다. 손으로 구현하면 두 규칙이 갈라져
    응답의 필드 이름이 요청의 필드 이름과 달라질 수 있다.

    `lines[0].sku_code`처럼 인덱스가 붙은 경로를 다루므로 세그먼트별로 이름 부분만
    바꾸고 대괄호는 그대로 둔다.
    """
    segments: list[str] = []
    for segment in path.split("."):
        name, bracket, index = segment.partition("[")
        segments.append(to_camel(name) + bracket + index)
    return ".".join(segments)

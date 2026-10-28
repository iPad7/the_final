"""코드 두 곳에 흩어진 같은 사실이 어긋나지 않는지 확인한다.

도메인은 SQLAlchemy를 import할 수 없어 길이 상한을 따로 들고 있고, Alembic
autogenerate는 import된 모델만 본다. 둘 다 조용히 어긋날 수 있는 지점이라 테스트로
묶어 둔다.
"""

from pathlib import Path

from sqlalchemy import String
from sqlalchemy.sql.expression import FromClause

import fulfill.modules
from fulfill.metadata import REGISTERED_MODEL_MODULES
from fulfill.modules.inventory.models import Sku
from fulfill.modules.requests.domain import (
    MAX_EXTERNAL_REQUEST_ID_LENGTH,
    MAX_RECIPIENT_NAME_LENGTH,
    MAX_RECIPIENT_PHONE_LENGTH,
    MAX_SELLER_CODE_LENGTH,
    MAX_SKU_CODE_LENGTH,
)
from fulfill.modules.requests.models import FulfillmentRequest
from fulfill.modules.seller_contracts.models import Seller


def _varchar_length(table: FromClause, column: str) -> int:
    """varchar 컬럼의 길이를 읽는다. String이 아니면 테스트가 바로 깨진다."""
    column_type = table.c[column].type
    assert isinstance(column_type, String), f"{column}은 varchar가 아닙니다"
    assert column_type.length is not None, f"{column}에 길이가 없습니다"
    return column_type.length


def test_도메인_길이_상한이_컬럼_길이와_일치한다() -> None:
    # 어느 한쪽만 바꾸면 과도한 입력이 검증을 통과한 뒤 INSERT에서 터진다.
    assert (
        _varchar_length(FulfillmentRequest.__table__, "external_request_id")
        == MAX_EXTERNAL_REQUEST_ID_LENGTH
    )
    assert (
        _varchar_length(FulfillmentRequest.__table__, "recipient_name") == MAX_RECIPIENT_NAME_LENGTH
    )
    assert (
        _varchar_length(FulfillmentRequest.__table__, "recipient_phone")
        == MAX_RECIPIENT_PHONE_LENGTH
    )
    assert _varchar_length(Seller.__table__, "code") == MAX_SELLER_CODE_LENGTH
    assert _varchar_length(Sku.__table__, "code") == MAX_SKU_CODE_LENGTH


def test_모든_모듈의_models가_metadata에_등록돼_있다() -> None:
    # 빠뜨리면 autogenerate가 그 테이블을 못 보고, 오류 없이 빈 마이그레이션이
    # 만들어진다. 조용한 실패라서 테스트로만 잡을 수 있다.
    modules_dir = Path(str(fulfill.modules.__file__)).parent
    on_disk = {path.parent.name for path in modules_dir.glob("*/models.py")}
    registered = {module.__name__.split(".")[-2] for module in REGISTERED_MODEL_MODULES}

    assert on_disk == registered

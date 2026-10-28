"""BC-01이 공개하는 조회 입구.

다른 모듈은 `models`를 import하지 않고 이 함수들을 호출한다. 그래서 ORM이 이
모듈 안에만 머문다(ADR23 §3).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from fulfill.modules.seller_contracts.models import Seller


def find_seller_id_by_code(session: Session, code: str) -> uuid.UUID | None:
    """운영자용 판매사 코드를 해석한다. None은 그런 판매사가 없다는 뜻이다."""
    return session.scalars(select(Seller.id).where(Seller.code == code)).one_or_none()

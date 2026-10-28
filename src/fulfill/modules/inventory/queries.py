"""BC-03이 공개하는 조회 입구."""

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from fulfill.modules.inventory.models import Sku


def find_sku_ids_by_codes(
    session: Session, seller_id: uuid.UUID, codes: Iterable[str]
) -> dict[str, uuid.UUID]:
    """판매사의 품목 코드들을 한 번의 질의로 해석한다.

    대응하는 SKU가 없는 코드는 결과에서 빠진다. 호출자는 그것으로 잘못된 요청
    라인을 알아낸다.
    """
    rows = session.execute(
        select(Sku.code, Sku.id).where(Sku.seller_id == seller_id, Sku.code.in_(list(codes)))
    ).all()
    return {code: sku_id for code, sku_id in rows}

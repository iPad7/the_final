"""데모용 가상 판매사·SKU를 넣는다.

판매사 온보딩과 품목 등록은 계약 조건을 다루는 기능이고 아직 구현되지 않았다.
이 스크립트는 그 자리를 임시로 메우는 개발 도구이므로 ORM을 직접 쓴다. 업무
모듈이 아니라서 모듈 경계 계약의 대상도 아니다.

SIMULATION 데이터만 넣는다(ADR23-08). 실제 수취인 정보는 다루지 않는다.

    uv run python -m scripts.seed_demo
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from fulfill.modules.inventory.models import Sku
from fulfill.modules.seller_contracts.models import Seller
from fulfill.shared.db import unit_of_work

SELLERS = (
    ("SELLER-A", "가상 판매사 A"),
    ("SELLER-B", "가상 판매사 B"),
)

# 버튜버·애니 IP 굿즈를 가정한 품목. 같은 코드가 두 판매사에 모두 있는 것은
# 의도적이다. 코드가 같아도 판매사가 다르면 다른 물건이라는 점을 데모에서
# 보여 주기 위한 것이다.
SKUS = (
    ("ACR-STAND", "아크릴 스탠드"),
    ("ACR-KEYRING", "아크릴 키링"),
    ("CAN-BADGE", "캔뱃지 세트"),
    ("PLUSH-S", "봉제인형 (소)"),
)


def _upsert_sellers(session: Session) -> dict[str, uuid.UUID]:
    """판매사를 넣고 코드 → id를 돌려준다.

    ON CONFLICT DO NOTHING으로 재실행을 허용한다. 두 번째 실행이 유니크 위반으로
    죽으면 개발 도구로 쓸 수 없다.
    """
    for code, name in SELLERS:
        session.execute(
            insert(Seller)
            .values(id=uuid.uuid4(), code=code, name=name)
            .on_conflict_do_nothing(index_elements=["code"])
        )
    # 이미 있던 행의 id가 필요하므로 넣은 뒤 다시 읽는다.
    rows = session.execute(select(Seller.code, Seller.id)).all()
    return {code: seller_id for code, seller_id in rows}


def _upsert_skus(session: Session, seller_ids: dict[str, uuid.UUID]) -> int:
    """판매사마다 같은 품목 코드를 넣는다.

    UNIQUE (seller_id, code) 덕분에 코드가 같아도 판매사가 다르면 별개 행이 된다.
    두 판매사의 재고를 섞어 쓸 수 없다는 점이 데이터에서 드러난다.
    """
    for seller_id in seller_ids.values():
        for code, name in SKUS:
            session.execute(
                insert(Sku)
                .values(id=uuid.uuid4(), seller_id=seller_id, code=code, name=name)
                .on_conflict_do_nothing(index_elements=["seller_id", "code"])
            )
    return session.execute(select(func.count()).select_from(Sku)).scalar_one()


def main() -> None:
    """판매사와 SKU를 한 트랜잭션에서 넣는다.

    중간에 실패하면 판매사만 들어가고 SKU는 없는 상태가 남으므로 한 묶음으로
    처리한다.
    """
    with unit_of_work() as session:
        seller_ids = _upsert_sellers(session)
        sku_count = _upsert_skus(session, seller_ids)

    print(f"판매사 {len(seller_ids)}곳, SKU {sku_count}건 준비 완료 (SIMULATION)")
    for code in seller_ids:
        print(f"  - {code}")


if __name__ == "__main__":
    main()

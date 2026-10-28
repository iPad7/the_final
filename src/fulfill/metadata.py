"""스키마 조합 지점: 모든 모듈의 테이블을 하나의 metadata에 모은다.

모델 모듈을 import하는 행위가 그 테이블을 `Base.metadata`에 등록하며, Alembic
autogenerate는 import된 것만 본다. 따라서 아래 목록에서 모듈이 빠지면 오류 없이
빈 마이그레이션이 만들어진다. 그래서 modules/*/models.py가 모두 여기 등장하는지
확인하는 테스트를 둔다.

`shared/`가 아니라 `main.py` 옆에 두는 이유는, `shared`는 작은 횡단 요소만 담고
업무 모듈에 의존해서는 안 되기 때문이다.
"""

from sqlalchemy import MetaData

from fulfill.modules.allocation import models as allocation_models
from fulfill.modules.inventory import models as inventory_models
from fulfill.modules.requests import models as requests_models
from fulfill.modules.seller_contracts import models as seller_contracts_models
from fulfill.shared.db import Base

# 위 import가 죽은 코드로 오인되지 않도록 참조해 둔다.
REGISTERED_MODEL_MODULES = (
    allocation_models,
    inventory_models,
    requests_models,
    seller_contracts_models,
)

target_metadata: MetaData = Base.metadata

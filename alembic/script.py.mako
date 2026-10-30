"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
## 블록 지시문을 쓴다. ${imports if imports else ""} 형태는 값이 비어도 그 줄의
## 개행이 남아 빈 줄이 하나 더 생긴다.
% if imports:
${imports}
% endif

# Alembic이 사용하는 리비전 식별자.
revision: str = ${repr(up_revision).replace("'", '"')}
down_revision: str | Sequence[str] | None = ${repr(down_revision).replace("'", '"')}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels).replace("'", '"')}
depends_on: str | Sequence[str] | None = ${repr(depends_on).replace("'", '"')}


def upgrade() -> None:
    """스키마 상향 적용."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """스키마 하향 복구."""
    ${downgrades if downgrades else "pass"}

"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

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

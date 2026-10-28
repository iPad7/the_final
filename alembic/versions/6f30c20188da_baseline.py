"""baseline

Empty on purpose. With no revision at all, `alembic upgrade head` does nothing
and not even alembic_version is created, so there would be no way to show that
migrations reach the database. Applying this one proves the path works against a
real PostgreSQL. The first real tables arrive in 24-1.

Revision ID: 6f30c20188da
Revises:
Create Date: 2026-10-27 23:46:13.398979

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "6f30c20188da"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No schema change; see the module docstring."""


def downgrade() -> None:
    """No schema change; see the module docstring."""

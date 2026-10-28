"""Alembic environment.

The database URL is resolved through `Settings`, not `alembic.ini`, so the local
compose database, the throwaway container used by the integration tests and CI
all read it the same way (DATABASE_URL in the environment wins).
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from fulfill.shared.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No ORM models exist yet; the first tables arrive in 24-1, so autogenerate has
# nothing to compare against until then.
target_metadata = None


def run_migrations_offline() -> None:
    """Emit migrations as SQL without connecting to a database."""
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a real connection.

    NullPool: a migration run is short-lived, so pooled connections would only
    keep the database busy after the process is done.
    """
    connectable = create_engine(get_settings().database_url, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

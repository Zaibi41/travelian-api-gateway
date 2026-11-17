"""Explicit migrations only. Never run during application startup."""

from getpass import getpass

from alembic import context
from sqlalchemy import create_engine, pool

from app.db.config import DatabaseSettings


def run_migrations() -> None:
    if context.is_offline_mode():
        context.configure(
            dialect_name="postgresql",
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    # Tests can inject an isolated connection, without reading local credentials.
    supplied = context.config.attributes.get("connection")
    if supplied is not None:
        context.configure(connection=supplied)
        with context.begin_transaction():
            context.run_migrations()
        return

    settings = DatabaseSettings()
    password = None if settings.db_password else getpass("Local database password: ")
    engine = create_engine(
        settings.url(password),
        poolclass=pool.NullPool,
        echo=False,
        hide_parameters=True,
        connect_args={"connect_timeout": 5},
    )
    try:
        with engine.connect() as connection:
            context.configure(connection=connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations()

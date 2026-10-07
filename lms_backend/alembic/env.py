"""
Alembic environment.

Runs migrations synchronously (psycopg2) even though the app is async —
migrations are a one-shot admin task and the sync driver keeps this file
simple and debuggable. Target metadata is imported from `app.models`, so
autogenerate sees every table.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from app.core.config import settings
from app.db.base_class import DB_SCHEMA

# Importing the package registers all mappers for autogenerate.
from app.models import Base  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings.sync_database_url)

target_metadata = Base.metadata


def include_object(object_, name, type_, reflected, compare_to):
    """
    Keep autogenerate focused on the LMS schema.

    The same physical database also holds the RAG chatbot's `public` tables;
    without this filter, autogenerate would happily propose dropping them.
    """
    if type_ == "table" and object_.schema not in (DB_SCHEMA, None):
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=settings.sync_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        include_schemas=True,
        include_object=include_object,
        version_table_schema=DB_SCHEMA,
        compare_type=True,
        compare_server_default=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        if DB_SCHEMA:
            # The version table lives in our schema, so it must exist before
            # Alembic tries to stamp anything.
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{DB_SCHEMA}"'))
            connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_object=include_object,
            version_table_schema=DB_SCHEMA,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

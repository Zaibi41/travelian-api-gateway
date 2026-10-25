import os
import uuid

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


@pytest.fixture
def database(monkeypatch):
    url = os.environ.get("TRAVELIAN_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "Set TRAVELIAN_TEST_DATABASE_URL to a disposable PostgreSQL database"
        )
    engine = sa.create_engine(url)
    with engine.connect() as connection:
        transaction = connection.begin()
        schema = "test_agency_" + uuid.uuid4().hex
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        metadata = sa.MetaData()
        metadata.reflect(connection)
        from contextlib import contextmanager

        from app.db import agencies

        class Engine:
            @contextmanager
            def begin(self):
                with connection.begin_nested():
                    yield connection

        monkeypatch.setattr(agencies, "application_engine", lambda: Engine())
        try:
            yield connection, metadata, config
        finally:
            transaction.rollback()
    engine.dispose()

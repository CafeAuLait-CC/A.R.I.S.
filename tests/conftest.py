import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from aris.apps.api.main import create_app
from aris.core.config import Settings

AGENT_TOKEN = "test-agent-credential-12345"
READ_TOKEN = "test-read-credential-12345"


@pytest.fixture
def database_url(tmp_path):
    postgres = os.environ.get("ARIS_TEST_DATABASE_URL")
    if not postgres:
        yield f"sqlite+pysqlite:///{tmp_path / 'test.db'}"
        return
    # Every test gets an isolated schema; never drop the user's database/tables.
    schema = f"aris_test_{uuid4().hex}"
    engine = create_engine(postgres)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = make_url(postgres).update_query_dict({"options": f"-csearch_path={schema}"})
    try:
        yield url.render_as_string(hide_password=False)
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


@pytest.fixture
def settings(database_url, monkeypatch):
    monkeypatch.setenv("ARIS_DATABASE_URL", database_url)
    monkeypatch.setenv("ARIS_ENVIRONMENT", "test")
    command.upgrade(Config("alembic.ini"), "head")
    return Settings(
        _env_file=None,
        environment="test",
        database_url=database_url,
        read_token=READ_TOKEN,
        agent_credentials={
            "agent-a": {"node_id": "node-a", "token": AGENT_TOKEN},
            "agent-b": {"node_id": "node-b", "token": "second-agent-credential-12345"},
        },
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        yield client

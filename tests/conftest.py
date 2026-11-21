import gc
import os
import shutil
from pathlib import Path
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
FROM_POSTGRES_TEMPLATE = pytest.StashKey[bool]()


def pytest_addoption(parser):
    parser.addoption(
        "--database-only",
        action="store_true",
        help="Run tests whose fixture closure uses the selected application database.",
    )
    parser.addoption(
        "--postgres-template",
        action="store_true",
        help="Clone a migrated template for settings-based tests (local aris_test only).",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--database-only"):
        selected, omitted = [], []
        for item in items:
            (selected if "database_url" in item.fixturenames else omitted).append(item)
        items[:] = selected
        config.hook.pytest_deselected(items=omitted)


def pytest_runtest_call(item):
    if (
        str(item.funcargs.get("database_url", "")).startswith("postgresql")
        and gc.get_count()[2] >= gc.get_threshold()[2]
    ):
        # Heavy migration fixtures can leave an old-generation collection due.
        # Perform it before exercising the application's short transactions.
        gc.collect()


@pytest.fixture(scope="session")
def sqlite_schema_template(tmp_path_factory):
    # Run the real migration chain once, then copy only this closed, empty
    # test database. Direct database_url users still receive an unmigrated DB.
    path = tmp_path_factory.mktemp("sqlite-schema-template") / "head.db"
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("ARIS_DATABASE_URL", f"sqlite+pysqlite:///{path}")
        environment.setenv("ARIS_ENVIRONMENT", "test")
        command.upgrade(Config("alembic.ini"), "head")
    return path


@pytest.fixture(scope="session")
def postgres_template(request):
    from tests.postgres_template import migrated_template

    value = os.environ.get("ARIS_TEST_DATABASE_URL")
    if not value:
        raise pytest.UsageError("--postgres-template requires ARIS_TEST_DATABASE_URL")
    with migrated_template(value) as template:
        yield template


@pytest.fixture
def database_url(tmp_path, request):
    postgres = os.environ.get("ARIS_TEST_DATABASE_URL")
    if not postgres:
        yield f"sqlite+pysqlite:///{tmp_path / 'test.db'}"
        return
    if request.config.getoption("--postgres-template") and "settings" in request.fixturenames:
        template = request.getfixturevalue("postgres_template")
        with template.clone() as selected:
            request.node.stash[FROM_POSTGRES_TEMPLATE] = True
            yield selected
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
def settings(database_url, monkeypatch, request, tmp_path):
    monkeypatch.setenv("ARIS_DATABASE_URL", database_url)
    monkeypatch.setenv("ARIS_ENVIRONMENT", "test")
    url = make_url(database_url)
    target = Path(url.database) if url.get_backend_name() == "sqlite" and url.database else None
    if (
        target is not None
        and target.resolve().is_relative_to(tmp_path.resolve())
        and not target.exists()
    ):
        shutil.copyfile(request.getfixturevalue("sqlite_schema_template"), target)
    elif not request.node.stash.get(FROM_POSTGRES_TEMPLATE, False):
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

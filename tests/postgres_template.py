"""Opt-in disposable database clones of one genuinely migrated empty test schema."""

import re
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.pool import NullPool


@dataclass(frozen=True)
class PostgresTemplate:
    maintenance: Engine
    base: URL
    database: str
    schema: str

    @contextmanager
    def clone(self):
        name = "aris_case_" + uuid4().hex
        with self.maintenance.connect() as db:
            db.execute(
                text(f'CREATE DATABASE "{name}" TEMPLATE "{self.database}" STRATEGY WAL_LOG')
            )
        try:
            yield (
                self.base.set(database=name)
                .update_query_dict({"options": "-csearch_path=" + self.schema})
                .render_as_string(hide_password=False)
            )
        finally:
            # Test services may leave idle pooled connections. This exact newly
            # generated database is owned by this fixture, never the base DB.
            assert re.fullmatch(r"aris_case_[a-f0-9]{32}", name)
            with self.maintenance.connect() as db:
                db.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


@contextmanager
def migrated_template(value: str):
    url = make_url(value)
    if (
        url.get_backend_name() != "postgresql"
        or url.database != "aris_test"
        or url.username != "aris_test"
        or url.host not in (None, "localhost", "127.0.0.1")
    ):
        raise pytest.UsageError("--postgres-template requires a local aris_test database and role")
    maintenance = create_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    name, schema = "aris_template_" + uuid4().hex, "aris_test_" + uuid4().hex
    created = False
    try:
        with maintenance.connect() as db:
            assert db.scalar(text("SELECT current_database()")) == "aris_test"
            assert db.scalar(
                text("SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname=current_user")
            )
            db.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0 STRATEGY WAL_LOG'))
            created = True
        selected = url.set(database=name).update_query_dict({"options": "-csearch_path=" + schema})
        engine = create_engine(selected, poolclass=NullPool)
        try:
            with engine.begin() as db:
                db.execute(text("CREATE EXTENSION btree_gist WITH SCHEMA public"))
                db.execute(text(f'CREATE SCHEMA "{schema}"'))
        finally:
            engine.dispose()
        with pytest.MonkeyPatch.context() as environment:
            environment.setenv("ARIS_DATABASE_URL", selected.render_as_string(hide_password=False))
            environment.setenv("ARIS_ENVIRONMENT", "test")
            command.upgrade(Config("alembic.ini"), "head")
            command.check(Config("alembic.ini"))
        with maintenance.connect() as db:
            db.execute(text(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS false'))
        yield PostgresTemplate(maintenance, url, name, schema)
    finally:
        if created:
            assert re.fullmatch(r"aris_template_[a-f0-9]{32}", name)
            with maintenance.connect() as db:
                db.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        maintenance.dispose()

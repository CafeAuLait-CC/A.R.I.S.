import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

SCHEMA_REVISION = "0010"


class Base(DeclarativeBase):
    pass


def build_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        kwargs = {"poolclass": StaticPool} if ":memory:" in url else {}
        engine = create_engine(url, connect_args={"check_same_thread": False}, **kwargs)

        @event.listens_for(engine, "connect")
        def sqlite_foreign_keys(connection: object, _: object) -> None:
            assert isinstance(connection, sqlite3.Connection)
            connection.execute("PRAGMA foreign_keys=ON")

        return engine
    return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def transaction_savepoint(session: Session) -> Iterator[None]:
    """Protect a write batch; pending owner work can flush before the savepoint."""
    connection = session.connection()
    if connection.dialect.name == "sqlite":
        driver = connection.connection.driver_connection
        if not isinstance(driver, sqlite3.Connection):
            raise RuntimeError("unsupported SQLite transaction driver")
        # sqlite3 legacy mode does not BEGIN for SELECT or SAVEPOINT. Without
        # this, releasing the first savepoint can commit before the owner does.
        if not driver.in_transaction:
            connection.exec_driver_sql("BEGIN")
    with session.begin_nested():
        yield


def check_database(engine: Engine) -> None:
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        if revision != SCHEMA_REVISION:
            raise RuntimeError("database migration required")

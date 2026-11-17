from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError


def transaction_budget(
    session: Session, lock_ms: int, statement_ms: int, transaction_ms: int
) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text(
                "SELECT set_config('lock_timeout', :lock, true), "
                "set_config('statement_timeout', :statement, true), "
                "set_config('transaction_timeout', :transaction, true)"
            ),
            {
                "lock": f"{lock_ms}ms",
                "statement": f"{statement_ms}ms",
                "transaction": f"{transaction_ms}ms",
            },
        )
    elif session.get_bind().dialect.name != "sqlite":
        raise DomainError("DEPENDENCY_UNAVAILABLE", "Unsupported command database", 503)


def database_failure(error: DBAPIError) -> str:
    state = getattr(error.orig, "sqlstate", None)
    if (
        state in {"40P01", "40001", "55P03", "57014", "25P04"}
        or error.connection_invalidated
        or (isinstance(state, str) and state.startswith("08"))
    ):
        return "RETRYABLE_DATABASE_ERROR"
    return "INTERNAL_ERROR"

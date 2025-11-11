import logging
import sys
from contextvars import ContextVar

# Context variable for request ID (optional)
_request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)


def set_request_id(req_id: str | None):
    """Set current request id (used for per-request logging)."""
    _request_id_ctx.set(req_id)


def get_request_id() -> str | None:
    return _request_id_ctx.get()


class RequestIDLogFilter(logging.Filter):
    def filter(self, record):
        record.request_id = get_request_id() or "-"
        return True


def _setup_root_logger():
    """Initialize root logger (called once on import)."""
    root = logging.getLogger()
    if root.handlers:
        # already configured (e.g. by uvicorn)
        return

    handler = logging.StreamHandler(sys.stdout)
    fmt = "[%(asctime)s] %(levelname)-7s [%(name)s] [%(request_id)s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(fmt=fmt, datefmt=datefmt)
    handler.setFormatter(formatter)
    handler.addFilter(RequestIDLogFilter())

    root.addHandler(handler)
    root.setLevel(logging.INFO)


def get_logger(name: str | None = None) -> logging.Logger:
    """Get a logger with unified format and request ID support."""
    _setup_root_logger()
    return logging.getLogger(name or "ARIS")


# Auto-init
_setup_root_logger()

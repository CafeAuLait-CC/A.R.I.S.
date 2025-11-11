import logging as _logging
import sys
from contextvars import ContextVar

# Context variable for request ID (optional)
_request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)


def set_request_id(req_id: str | None):
    """Set current request id (used for per-request logging)."""
    _request_id_ctx.set(req_id)


def get_request_id() -> str | None:
    return _request_id_ctx.get()


class RequestIDLogFilter(_logging.Filter):
    def filter(self, record):
        record.request_id = get_request_id() or "-"
        return True


class HeartbeatFilter(_logging.Filter):
    def filter(self, record: _logging.LogRecord) -> bool:
        # record.args 通常是 ('POST /internal/gpu/session/heartbeat HTTP/1.1', status_code)
        msg = str(record.getMessage())
        if "/internal/gpu/session/heartbeat" in msg:
            return False
        return True


def mute_heartbeat_access_log():
    logger = _logging.getLogger("uvicorn.access")
    logger.addFilter(HeartbeatFilter())


def _setup_root_logger():
    """Initialize root logger (called once on import)."""
    root = _logging.getLogger()
    if root.handlers:
        # already configured (e.g. by uvicorn)
        return

    handler = _logging.StreamHandler(sys.stdout)
    fmt = "[%(asctime)s] %(levelname)-7s [%(name)s] [%(request_id)s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    formatter = _logging.Formatter(fmt=fmt, datefmt=datefmt)
    handler.setFormatter(formatter)
    handler.addFilter(RequestIDLogFilter())

    root.addHandler(handler)
    root.setLevel(_logging.INFO)


def get_logger(name: str | None = None) -> _logging.Logger:
    """Get a logger with unified format and request ID support."""
    _setup_root_logger()
    return _logging.getLogger(name or "ARIS")


# Auto-init
_setup_root_logger()

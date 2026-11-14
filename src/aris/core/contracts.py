from typing import Literal

from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorDetail(Contract):
    code: str
    message: str
    request_id: str


class ErrorResponse(Contract):
    ok: Literal[False] = False
    error: ErrorDetail


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)

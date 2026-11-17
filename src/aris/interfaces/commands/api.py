from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from aris.core.access.schemas import AccessContract
from aris.core.commands.gateway_schemas import UserCommandInput
from aris.core.commands.queue_schemas import IntakeResult, WithdrawCommand
from aris.core.commands.schemas import CommandOutcome, ConfirmCommand, PendingConfirmation
from aris.core.contracts import DomainError
from aris.interfaces.commands.service import UserCommandSurface


class ConfirmationInput(AccessContract):
    proof: str = Field(min_length=64, max_length=64, repr=False)


def outcome_json(outcome: CommandOutcome) -> dict[str, Any]:
    data = outcome.model_dump(mode="json")
    if isinstance(outcome, PendingConfirmation):
        # The owning authenticated user needs this bounded confirmation proof;
        # it is never part of persistence or logs.
        data["proof"] = outcome.proof.get_secret_value()
    return data


def intake_response(result: IntakeResult) -> JSONResponse:
    data = result.model_dump(mode="json")
    if result.outcome is not None:
        data["outcome"] = outcome_json(result.outcome)
    status = (
        (result.error_status or 500)
        if result.disposition == "rejected"
        else (
            202
            if result.outcome is not None
            and result.outcome.state in {"awaiting_confirmation", "queued"}
            else 200
        )
    )
    return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})


async def bounded_input[Model: BaseModel](request: Request, model: type[Model]) -> Model:
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise DomainError("VALIDATION_ERROR", "JSON request body is required", 415)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 32768:
            raise DomainError("VALIDATION_ERROR", "Command request is too large", 413)
    try:
        return model.model_validate_json(bytes(body))
    except ValidationError:
        raise DomainError("VALIDATION_ERROR", "Invalid command request", 422) from None


def request_key(request: Request) -> str:
    keys = request.headers.getlist("idempotency-key")
    key = keys[0] if len(keys) == 1 else ""
    if not 1 <= len(key) <= 128:
        raise DomainError("VALIDATION_ERROR", "Idempotency-Key is required", 422)
    return key


def authorization(request: Request) -> str | None:
    values = request.headers.getlist("authorization")
    return values[0] if len(values) == 1 else None


def build_router(service: UserCommandSurface) -> APIRouter:
    router = APIRouter(prefix="/internal/commands", tags=["user-commands"])

    @router.post("")
    async def submit(request: Request) -> JSONResponse:
        payload = await bounded_input(request, UserCommandInput)
        result = await run_in_threadpool(
            service.submit,
            authorization(request),
            request.state.request_id,
            request_key(request),
            payload,
        )
        return intake_response(result)

    @router.post("/{command_id}/confirm")
    async def confirm(command_id: UUID, request: Request) -> JSONResponse:
        payload = await bounded_input(request, ConfirmationInput)
        result = await run_in_threadpool(
            service.confirm,
            authorization(request),
            request.state.request_id,
            request_key(request),
            ConfirmCommand(command_id=command_id, proof=payload.proof),
        )
        return intake_response(result)

    @router.post("/{command_id}/withdraw")
    async def withdraw(command_id: UUID, request: Request) -> JSONResponse:
        # A withdrawal has no body fields that can redirect actor or target.
        payload = await bounded_input(request, AccessContract)
        assert payload is not None
        result = await run_in_threadpool(
            service.withdraw,
            authorization(request),
            request.state.request_id,
            request_key(request),
            WithdrawCommand(command_id=command_id),
        )
        return intake_response(result)

    @router.get("/{command_id}")
    async def inspect(command_id: UUID, request: Request) -> JSONResponse:
        result = await run_in_threadpool(
            service.inspect,
            authorization(request),
            request.state.request_id,
            command_id,
        )
        return JSONResponse(outcome_json(result), headers={"Cache-Control": "no-store"})

    return router

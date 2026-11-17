import re

from fastapi import APIRouter, Request
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from aris.core.contracts import DomainError
from aris.interfaces.commands.api import authorization
from aris.interfaces.reservations.service import UserReservationSurface
from aris.modules.nova.reservation_schemas import MineCursor, MineQuery


def mine_query(request: Request) -> MineQuery:
    pairs = request.query_params.multi_items()
    values = dict(pairs)
    if (
        len(values) != len(pairs)
        or values.keys() - {"limit", "before_created_at", "before_id"}
        or any(len(value) > 128 for value in values.values())
        or ("before_created_at" in values) != ("before_id" in values)
    ):
        raise DomainError("VALIDATION_ERROR", "Invalid personal reservation query", 422)
    limit = values.get("limit", "20")
    if re.fullmatch(r"[1-9][0-9]?", limit, re.ASCII) is None:
        raise DomainError("VALIDATION_ERROR", "Invalid personal reservation query", 422)
    try:
        cursor = (
            MineCursor.model_validate(
                {"created_at": values["before_created_at"], "reservation_id": values["before_id"]}
            )
            if "before_id" in values
            else None
        )
        return MineQuery(limit=int(limit), cursor=cursor)
    except ValidationError:
        raise DomainError("VALIDATION_ERROR", "Invalid personal reservation query", 422) from None


def build_router(service: UserReservationSurface) -> APIRouter:
    router = APIRouter(prefix="/internal/reservations", tags=["user-reservations"])

    @router.get("/mine")
    async def mine(request: Request) -> JSONResponse:
        result = await run_in_threadpool(
            service.mine, authorization(request), request.state.request_id, mine_query(request)
        )
        return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    return router

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ...core.db import get_db
from ...modules.gpu import schemas
from ...modules.gpu.service import gpu_service

router = APIRouter(prefix="/gpu", tags=["internal-gpu"])


@router.post("/register")
def gpu_register(
    payload: schemas.AgentRegisterRequest,
    db: Session = Depends(get_db),
):
    result = gpu_service.handle_register(db, payload)
    if not result.get("ok"):
        raise HTTPException(status_code=403, detail=result.get("detail", "invalid token"))
    return result


@router.post("/session/start")
def gpu_session_start(
    payload: schemas.AgentSessionStartRequest,
    db: Session = Depends(get_db),
):
    resp = gpu_service.handle_session_start(db, payload)
    if not resp.ok:
        raise HTTPException(status_code=403, detail=resp.detail or "invalid request")
    return resp


@router.post("/session/heartbeat")
def gpu_session_heartbeat(
    payload: schemas.AgentSessionHeartbeatRequest,
    db: Session = Depends(get_db),
):
    resp = gpu_service.handle_session_heartbeat(db, payload)
    if not resp.ok:
        raise HTTPException(status_code=403, detail=resp.detail or "invalid request")
    return resp


@router.post("/session/end")
def gpu_session_end(
    payload: schemas.AgentSessionEndRequest,
    db: Session = Depends(get_db),
):
    resp = gpu_service.handle_session_end(db, payload)
    # Handle non-successful states gracefully instead of throwing 4xx errors directly, 
    # to prevent the agent from getting stuck due to state desynchronization.
    
    # TODO: ADD loggings: 
    # if not resp.ok:
    #     logger.warning(f"[GPU-END] {payload.gpu_uuid}/{payload.user}: {resp.detail}")
    return resp

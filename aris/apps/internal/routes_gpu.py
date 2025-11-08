from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ...core.db import get_db
from ...modules.gpu.schemas import GpuReport
from ...modules.gpu.service import gpu_service

router = APIRouter()

@router.post("/gpu/report")
async def gpu_report(
    payload: GpuReport,
    db: Session = Depends(get_db),
):
    ok = gpu_service.handle_report(db, payload)
    if not ok:
        raise HTTPException(status_code=403, detail="invalid node or token")
    return {"ok": True}

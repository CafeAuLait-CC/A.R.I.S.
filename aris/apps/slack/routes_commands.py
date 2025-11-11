from fastapi import APIRouter, Request, Depends
from urllib.parse import parse_qs
from sqlalchemy.orm import Session

from ...core.db import get_db
from ...core.auth import get_or_create_user
from ...core.logging import get_logger
from ...modules.gpu.service import gpu_service

router = APIRouter()
logger = get_logger("aris.slack")


@router.post("/commands")
async def slack_commands(
    request: Request,
    db: Session = Depends(get_db),
):
    body = await request.body()
    # deps.verify_slack_signature）

    form = parse_qs(body.decode())
    command = form.get("command", [""])[0]
    text = form.get("text", [""])[0]
    slack_user_id = form.get("user_id", [""])[0]
    user_name = form.get("user_name", [""])[0]

    user = get_or_create_user(db, slack_user_id, user_name)

    if command == "/gpu":
        resp_text = gpu_service.handle_slash(db, user, text)
    else:
        resp_text = f"未知命令：{command}"

    # Slack requires text or JSON
    return {"response_type": "ephemeral", "text": resp_text}

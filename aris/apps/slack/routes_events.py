import json
from urllib.parse import parse_qs
from fastapi import APIRouter, Request, Response, Header, BackgroundTasks
from typing import Optional

from ...modules.gpu.service import gpu_service
from ...core.db import SessionLocal
from ...core.slack import (
    slack_client,
    verify_slack_signature,
    safe_chat_post_message,
)

router = APIRouter(tags=["slack-events"])


def reply_message(channel: str, text: str = "", thread_ts: Optional[str] = None):
    """Background Task: Simple Echo"""
    reply = (text or "").strip() or "👋 Hi!"
    safe_chat_post_message(
        channel=channel,
        text=f"Received: {reply}",
        thread_ts=thread_ts,
    )


def build_home_view(user_id: str):
    """Pull real-time GPU status from DB"""
    db = SessionLocal()
    try:
        cluster = gpu_service.get_realtime_cluster_view(db)
    finally:
        db.close()

    updated_at = cluster["updated_at"]
    updated_str = updated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")

    total = cluster["total_gpus"]
    active = cluster["active_gpus"]
    idle = cluster["idle_gpus"]

    blocks: list[dict] = []

    # 1) Welcome
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"👋 Welcome back, <@{user_id}>!",
            },
        }
    )

    blocks.append({"type": "divider"})

    # 2) System Status (Based on real-time summary)
    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "🧠 *System Status*"},
        }
    )

    if total == 0:
        status_text = "No GPUs registered yet."
    else:
        status_text = (
            f"Nodes / GPUs: `{total}` GPUs  ·  Active: `{active}`  ·  Idle: `{idle}`"
        )

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": status_text,
                }
            ],
        }
    )

    blocks.append({"type": "divider"})

    # 3) GPU status of each node
    for node in cluster["nodes"]:
        hostname = node["hostname"]
        gpus = node["gpus"]

        # Node header
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{hostname}*",
                },
            }
        )

        if not gpus:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "_No GPUs discovered on this node._",
                        }
                    ],
                }
            )
        else:
            for gpu in gpus:
                if gpu["state"] == "in_use":
                    emoji = "🔴"
                else:
                    emoji = "🟢"

                mem = f"{gpu['memory_mb']}MB" if gpu["memory_mb"] else "memory N/A"

                line = (
                    f"{emoji} GPU-{gpu['index']} · {gpu['name']} ({mem})"
                    f" · {gpu['summary']}"
                )

                blocks.append(
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": line,
                            }
                        ],
                    }
                )

        blocks.append({"type": "divider"})

    # 4) Quick Actions（Two simple actions for now）
    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "⚙️ *Quick Actions*"},
        }
    )

    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔁 Refresh"},
                    "action_id": "refresh_home",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "📊 Open Dashboard"},
                    "url": "https://example.com/aris/dashboard",  # TODO: replace with real url.
                },
            ],
        }
    )

    # 5) Footer: Last updated & Version (Version number should read from config）
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Last updated: {updated_str}",
                },
                {
                    "type": "mrkdwn",
                    "text": "Version: `v0.1.0-prototype`",  # TODO: load from settings.
                },
            ],
        }
    )

    return {
        "type": "home",
        "callback_id": "aris_home",
        "blocks": blocks,
    }


def publish_home_tab(user_id: str):
    """Publish / Update Home Tab."""
    try:
        slack_client.views_publish(
            user_id=user_id,
            view=build_home_view(user_id),
        )
    except Exception as e:
        print(f"[Slack] views_publish error: {e}")


async def handle_slack_events(
    request: Request,
    background: BackgroundTasks,
    x_slack_signature: str = Header(None),
    x_slack_request_timestamp: str = Header(None),
):
    raw = await request.body()

    # Verify Slack Signature
    if not verify_slack_signature(raw, x_slack_signature, x_slack_request_timestamp):
        return Response(status_code=401)

    content_type = request.headers.get("content-type", "")

    # ============ 1) Handle Interactivity Event (Refresh Button) ============
    if "application/x-www-form-urlencoded" in content_type:
        # Slack interactive payload: payload=<json>
        qs = parse_qs(raw.decode())
        payload_raw = qs.get("payload", ["{}"])[0]
        data = json.loads(payload_raw)

        if data.get("type") == "block_actions":
            user = data.get("user", {}) or {}
            user_id = user.get("id")
            actions = data.get("actions", []) or []

            for act in actions:
                if act.get("action_id") == "refresh_home" and user_id:
                    # Background refresh Home Tab
                    background.add_task(publish_home_tab, user_id)
                    break

        # Immediate return 200 for interactivity requests
        return {"ok": True}

    # ============ 2) handle general event subscriptions ============
    data = json.loads(raw.decode())

    # URL verification
    if data.get("type") == "url_verification":
        return {"challenge": data.get("challenge")}

    # Events
    if data.get("type") == "event_callback":
        event = data.get("event", {}) or {}
        etype = event.get("type")
        subtype = event.get("subtype")

        # Ignore bot messages and some type of messages
        if event.get("bot_id") or subtype in {"message_changed", "message_deleted"}:
            return {"ok": True}

        # 1) @mentioned: app_mention
        if etype == "app_mention":
            channel = event["channel"]
            text = event.get("text", "")
            thread_ts = event.get("thread_ts") or event.get("ts")
            background.add_task(reply_message, channel, text, thread_ts)

            # 2) DMs: message.im
        elif etype == "message" and event.get("channel_type") == "im":
            channel = event["channel"]
            text = event.get("text", "")
            thread_ts = event.get("thread_ts")
            background.add_task(reply_message, channel, text, thread_ts)

        # 3) App Home Opened
        elif etype == "app_home_opened":
            user_id = event.get("user")
            if user_id:
                background.add_task(publish_home_tab, user_id)

        else:
            # Ignore other types for now
            print(f"[Slack] Unhandled event type: {etype}")

    # Slack needs 200 response ASAP
    return {"ok": True}


@router.post("/events")
async def slack_events(
    request: Request,
    background: BackgroundTasks,
    x_slack_signature: str = Header(None),
    x_slack_request_timestamp: str = Header(None),
):
    return await handle_slack_events(
        request, background, x_slack_signature, x_slack_request_timestamp
    )

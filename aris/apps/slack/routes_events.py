from fastapi import APIRouter, Request, Response, Header, BackgroundTasks
from typing import Optional

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
    """Simple Home Tab Demo"""
    return {
        "type": "home",
        "callback_id": "home_view",
        "blocks": [
            # Header
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"👋 *Welcome back, <@{user_id}>!*",
                },
            },
            {"type": "divider"},
            # System info (TODO: replace with real query result)
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "🧠 *System Status:*\n"
                        "- GPU Usage: `32%`\n"
                        "- Running Jobs: `2`\n"
                        "- Idle Time: `15 min`"
                    ),
                },
            },
            {"type": "divider"},
            # Quick Actions (Placeholder, use real function in the future)
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "⚙️ *Quick Actions*"},
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Start Job"},
                        "style": "primary",
                        "action_id": "start_job",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Check Logs"},
                        "action_id": "check_logs",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open Dashboard"},
                        "url": "https://your-dashboard.example.com",
                    },
                ],
            },
            {"type": "divider"},
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "Last updated: 2025-11-02 21:00"},
                    {"type": "mrkdwn", "text": "Version: `v0.1.3-prototype`"},
                ],
            },
        ],
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

    if not verify_slack_signature(raw, x_slack_signature, x_slack_request_timestamp):
        return Response(status_code=401)

    data = await request.json()

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


@router.post("/slack/events")
async def slack_events(
    request: Request,
    background: BackgroundTasks,
    x_slack_signature: str = Header(None),
    x_slack_request_timestamp: str = Header(None),
):
    return await handle_slack_events(
        request, background, x_slack_signature, x_slack_request_timestamp
    )

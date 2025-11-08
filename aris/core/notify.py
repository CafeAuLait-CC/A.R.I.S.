from slack_sdk import WebClient
from .config import settings

client = WebClient(token=settings.SLACK_BOT_TOKEN)

class NotificationService:
    def __init__(self, client: WebClient):
        self.client = client

    def notify_user(self, slack_id: str, text: str):
        # simplified: send DM or send to exisiting conversation
        return self.client.chat_postMessage(channel=slack_id, text=text)

    def notify_channel(self, channel_id: str, text: str):
        return self.client.chat_postMessage(channel=channel_id, text=text)

notify = NotificationService(client)

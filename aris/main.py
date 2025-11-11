from fastapi import FastAPI

from aris.apps.slack.main import app as slack_app
from aris.apps.internal.main import app as internal_app

from aris.core.logging import mute_heartbeat_access_log

app = FastAPI(title="A.R.I.S.")

app.mount("/slack", slack_app)
app.mount("/internal", internal_app)

mute_heartbeat_access_log()

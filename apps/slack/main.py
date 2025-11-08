from fastapi import FastAPI, Request

app = FastAPI()


@app.get("/health")
async def root():
    return {"status": "OK"}


@app.post("/slack/commands")
async def slack_command(request: Request):
    body = await request.body()
    body_str = body.decode("utf-8")
    return {"text": "command received: " + body_str}

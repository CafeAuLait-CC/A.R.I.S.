from fastapi import FastAPI

# make sure SQLAlchemy Base has registered all tables
from ...modules.gpu import models as gpu_models
from ...core.db import Base, engine
from . import routes_commands

# temporary keep for local test 
Base.metadata.create_all(bind=engine)

app = FastAPI(title="ARIS Slack Gateway")


app.include_router(routes_commands.router)

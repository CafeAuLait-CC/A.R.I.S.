from fastapi import FastAPI

from ...modules.gpu import models as gpu_models
from ...core.db import Base, engine
from . import routes_gpu

Base.metadata.create_all(bind=engine)

app = FastAPI(title="ARIS Internal API")

app.include_router(routes_gpu.router)

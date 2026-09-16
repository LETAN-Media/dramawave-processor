import logging

from fastapi import FastAPI

from app.api.routes import router
from app.config import settings
from app.db import init_db


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
)

app = FastAPI(title='Bilibili Processor', version='0.1.0')
app.include_router(router)


@app.on_event('startup')
def startup() -> None:
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    init_db()

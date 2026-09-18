import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.dramawave import router as dramawave_router
from app.api.routes import router
from app.api.youtube import router as youtube_router
from app.config import settings
from app.db import init_db
from app.web.routes import router as studio_router


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
)

app = FastAPI(title='DramaWave Processor', version='0.1.0')
app.include_router(router)
app.include_router(dramawave_router)
app.include_router(youtube_router)
app.include_router(studio_router)
app.mount('/static', StaticFiles(directory=str(Path(__file__).parent / 'web' / 'static')), name='static')


@app.on_event('startup')
def startup() -> None:
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    init_db()

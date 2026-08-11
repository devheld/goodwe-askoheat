from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .askoheat_client import AskoHeatController
from .config import settings
from .controller import SurplusController
from .database import HistoryStore
from .goodwe_client import GoodWeCloudReader

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

controller: SurplusController | None = None
store: HistoryStore | None = None
worker_thread: threading.Thread | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global controller, store, worker_thread

    store = HistoryStore(settings.db_path)
    reader = GoodWeCloudReader(
        settings.sems_account,
        settings.sems_password,
        settings.station_id,
        settings.zero_threshold_kw,
        settings.battery_discharge_margin,
    )
    askoheat = AskoHeatController(
        settings.askoheat_host,
        settings.askoheat_port,
        settings.askoheat_step_register,
        settings.askoheat_load_register,
        settings.askoheat_temp_register,
        settings.askoheat_slave_id,
        settings.askoheat_delay_s,
    )
    controller = SurplusController(settings, reader, askoheat, store)

    worker_thread = threading.Thread(target=controller.run_forever, daemon=True)
    worker_thread.start()
    logger.info("Control loop started (interval: %ss)", settings.poll_interval_s)

    yield

    controller.stop()


app = FastAPI(title="AskoHeat Manager", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/live")
async def live() -> dict:
    if controller is None or controller.latest is None:
        raise HTTPException(status_code=503, detail="No data received yet")
    return {**controller.latest, "poll_interval_s": settings.poll_interval_s}


@app.get("/api/history")
async def history(hours: int = Query(24, ge=1, le=24 * 30)) -> dict:
    assert store is not None
    return {"hours": hours, "points": store.history(hours)}

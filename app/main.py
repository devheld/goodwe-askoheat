from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .askoheat_client import AskoHeatController
from .config import settings
from .controller import SurplusController
from .database import PERIODS, HistoryStore
from .goodwe_client import GoodWeModbusReader
from .wallbox_client import GoodWeWallboxReader

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
    reader = GoodWeModbusReader(
        settings.goodwe_host,
        settings.goodwe_port,
        settings.goodwe_slave_id,
        settings.zero_threshold_kw,
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
    wallbox = (
        GoodWeWallboxReader(
            settings.wallbox_host, settings.wallbox_port, settings.wallbox_slave_id
        )
        if settings.wallbox_host
        else None
    )
    controller = SurplusController(settings, reader, askoheat, store, wallbox)

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


@app.get("/energy")
async def energy_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "energy.html")


@app.get("/api/energy")
async def energy(period: str = Query("today")) -> dict:
    assert store is not None
    if period not in PERIODS:
        raise HTTPException(status_code=400, detail=f"Unknown period: {period}")
    return store.energy_buckets(period)


@app.get("/api/wallbox/sessions")
async def wallbox_sessions(days: int = Query(30, ge=1, le=366)) -> dict:
    assert store is not None
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    sessions = store.wallbox_sessions(since)
    return {
        "days": days,
        "sessions": sessions,
        "total_kwh": round(sum(s["energy_kwh"] for s in sessions), 3),
    }


@app.get("/api/wallbox/events")
async def wallbox_events(
    days: int = Query(30, ge=1, le=366), limit: int = Query(200, ge=1, le=1000)
) -> dict:
    assert store is not None
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return {"days": days, "events": store.wallbox_events(since, limit)}

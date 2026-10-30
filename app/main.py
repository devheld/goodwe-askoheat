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
from .shelly_client import ShellyClient
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

    store = HistoryStore(
        settings.db_path,
        # The loss buckets need the same limits the control loop runs on:
        # what counts as a full battery, how hot the tank may be before a
        # heating cycle may start, and where the heater runs out of steps.
        battery_capacity_kwh=settings.battery_capacity_kwh,
        battery_full_soc_pct=settings.battery_full_soc_pct,
        heating_start_max_temp_c=settings.heating_start_max_temp_c,
        step_max=settings.step_max,
        zero_threshold_kw=settings.zero_threshold_kw,
        # Also the rule for finding a charge in the record afterwards, since
        # a charge is exactly what stops the charger being readable: these
        # two decide when it was not read, and therefore where energy its
        # counter reports has to have come from.
        wallbox_busy_load_kw=settings.wallbox_busy_load_kw,
        wallbox_idle_ticks=settings.wallbox_idle_ticks,
    )
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
    shelly = (
        ShellyClient(
            settings.shelly_host,
            settings.shelly_port,
            settings.shelly_timeout_s,
            settings.shelly_role,
        )
        if settings.shelly_host
        else None
    )
    controller = SurplusController(settings, reader, askoheat, store, wallbox, shelly)

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
    return {
        **controller.latest,
        "poll_interval_s": settings.poll_interval_s,
        "tariff": _tariff(),
    }


@app.get("/api/history")
async def history(hours: int = Query(24, ge=1, le=24 * 30)) -> dict:
    assert store is not None
    return {"hours": hours, "points": store.history(hours)}


@app.get("/energy")
async def energy_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "energy.html")


def _tariff() -> dict:
    return {
        "currency": "CHF",
        "import_per_kwh": settings.price_import_chf_per_kwh,
        "export_per_kwh": settings.price_export_chf_per_kwh,
    }


@app.get("/api/energy")
async def energy(period: str = Query("today")) -> dict:
    assert store is not None
    if period not in PERIODS:
        raise HTTPException(status_code=400, detail=f"Unknown period: {period}")
    payload = store.energy_buckets(period)

    # Money is derived here rather than in the store: prices are
    # configuration, the store only knows what was measured.
    totals = payload["totals"]
    buy = settings.price_import_chf_per_kwh
    sell = settings.price_export_chf_per_kwh
    # Solar that never reached the grid is valued at the import price,
    # because that is exactly the purchase it replaced.
    self_consumed = max(0.0, totals["consumption"] - totals["grid_import"])
    payload["tariff"] = _tariff()
    payload["costs"] = {
        "spent": round(totals["grid_import"] * buy, 2),
        "earned": round(totals["grid_export"] * sell, 2),
        "avoided": round(self_consumed * buy, 2),
        "saved": round(self_consumed * buy + totals["grid_export"] * sell, 2),
        "consumption_value": round(totals["consumption"] * buy, 2),
        "self_consumed_kwh": round(self_consumed, 3),
    }

    # Losses are priced at the import tariff, not the export tariff: with
    # this installation exporting 0.6% of its production, a kWh that was lost
    # or held back is a kWh that had to be bought instead, not one that failed
    # to be sold.
    losses = payload["losses"]
    losses["real_chf"] = round(losses["real_kwh"] * buy, 2)
    losses["curtailed_chf"] = round(losses["curtailed_kwh"] * buy, 2)
    losses["headroom_chf"] = round(losses["curtailed_split"]["headroom"] * buy, 2)
    return payload


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

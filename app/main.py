from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from math import ceil
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
        # And the clamp's own threshold, for the part of the history that
        # has a clamp behind it: there the charger does not have to be
        # inferred from anything, only told apart from its own standby.
        wallbox_meter_busy_kw=settings.wallbox_meter_busy_kw,
        # And for the heat pump: what counts as its compressor running, and
        # the hours it is meant to run in. Neither can be enforced from
        # here - see heatpump_runs - they only decide what the record is
        # compared against.
        heatpump_run_kw=settings.heatpump_run_kw,
        heatpump_window=(
            settings.heatpump_window_from_h,
            settings.heatpump_window_to_h,
        ),
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
    # One clamp meter per question, each one optional. What a meter is for
    # is the name it is configured under: that name decides the sign it is
    # decoded with and the columns it is written to.
    shelly_meters = {
        role: ShellyClient(host, settings.shelly_port, settings.shelly_timeout_s, role)
        for role, host in (
            ("grid", settings.shelly_grid_host),
            ("heatpump", settings.shelly_heatpump_host),
            ("wallbox", settings.shelly_wallbox_host),
        )
        if host
    }
    controller = SurplusController(
        settings, reader, askoheat, store, wallbox, shelly_meters
    )

    worker_thread = threading.Thread(target=controller.run_forever, daemon=True)
    worker_thread.start()
    logger.info("Control loop started (interval: %ss)", settings.poll_interval_s)

    yield

    controller.stop()


app = FastAPI(title="Home Energy Manager", lifespan=lifespan)
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


# The heater and the charger have a page each rather than three screens of
# tiles on the dashboard. All of them read the same two endpoints; which
# charts a page draws is decided by the containers in its HTML.
@app.get("/asko")
async def asko_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "asko.html")


@app.get("/wallbox")
async def wallbox_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "wallbox.html")


def _tariff() -> dict:
    return {
        "currency": "CHF",
        "import_per_kwh": settings.price_import_chf_per_kwh,
        "export_per_kwh": settings.price_export_chf_per_kwh,
    }


def _benchmark(
    recorded_days: float, pv_kwh: float, returned_chf: float, buy: float, sell: float
) -> dict | None:
    """What the quote said the installation would do, measured against it.

    The quote is a straight line by construction - a fixed annual return
    from the first year - so it is used as one. The comparison is made over
    the days that were actually recorded rather than the days that have
    elapsed, because those differ by a week here and the quote has no
    opinion about a service outage.

    The quoted energy plan is priced twice on purpose. At the quote's own
    tariffs it reproduces the figure the quote printed, which is what its
    payback claim rests on. At this household's tariffs it becomes what
    that same plan would have earned here, and only that version can be
    set against the measured counter: compared across two different
    tariffs, the ratio would be measuring the price of electricity rather
    than the performance of the roof.
    """
    year_kwh = settings.benchmark_kwh_per_year
    if year_kwh <= 0:
        return None
    self_kwh = min(settings.benchmark_self_kwh_per_year, year_kwh)
    export_kwh = max(0.0, year_kwh - self_kwh)
    # Priced at the quote's own tariffs, not at PRICE_*: a promise is
    # compared at the prices it was made at, or the comparison measures the
    # tariff rather than the roof.
    chf_year = (
        self_kwh * settings.benchmark_import_chf_per_kwh
        + export_kwh * settings.benchmark_export_chf_per_kwh
    )
    chf_year_mine = self_kwh * buy + export_kwh * sell
    kwh_day = year_kwh / 365.0
    expected_kwh = kwh_day * recorded_days
    expected_chf = chf_year / 365.0 * recorded_days
    expected_chf_mine = chf_year_mine / 365.0 * recorded_days

    when = None
    if settings.investment_date and settings.benchmark_payback_years > 0:
        try:
            when = date.fromisoformat(settings.investment_date) + timedelta(
                days=round(settings.benchmark_payback_years * 365.25)
            )
        except ValueError:
            when = None

    return {
        "label": settings.benchmark_label or "Quote",
        "kwp": settings.benchmark_kwp,
        "kwh_per_year": round(year_kwh, 1),
        "self_kwh_per_year": round(self_kwh, 1),
        "export_kwh_per_year": round(export_kwh, 1),
        "import_chf_per_kwh": settings.benchmark_import_chf_per_kwh,
        "export_chf_per_kwh": settings.benchmark_export_chf_per_kwh,
        "chf_per_year": round(chf_year, 2),
        "chf_per_year_at_my_tariffs": round(chf_year_mine, 2),
        "chf_per_day": round(chf_year / 365.0, 2),
        "kwh_per_day": round(kwh_day, 2),
        "net_chf": round(settings.benchmark_net_chf, 2),
        "payback_years": settings.benchmark_payback_years,
        "break_even_date": when.isoformat() if when else None,
        "expected_kwh_to_date": round(expected_kwh, 1),
        "expected_chf_to_date": round(expected_chf, 2),
        "expected_chf_to_date_at_my_tariffs": round(expected_chf_mine, 2),
        "production_pct": round(100.0 * pv_kwh / expected_kwh, 1) if expected_kwh > 0 else None,
        # Against the plan priced at this household's tariffs, so the ratio
        # is about the roof and the share kept in the house - not about the
        # 15% the two tariff sheets happen to differ by.
        "return_pct": (
            round(100.0 * returned_chf / expected_chf_mine, 1) if expected_chf_mine > 0 else None
        ),
    }


def _oil_equivalent() -> dict:
    """How many litres of heating oil a kWh of the pump's electricity is.

    Derived here rather than in the store for the same reason money is:
    the store knows what was measured, and this is three assumptions. It
    travels with every payload that carries the pump's kWh so the page can
    state the assumptions beside the number instead of implying it was
    read off something.
    """
    usable = settings.oil_kwh_per_litre * settings.oil_boiler_efficiency
    return {
        # Heat per kWh of electricity, divided by the heat a litre of oil
        # actually delivered through an old boiler.
        "litres_per_kwh": round(settings.heatpump_spf / usable, 4) if usable > 0 else 0.0,
        "spf": settings.heatpump_spf,
        "kwh_per_litre": settings.oil_kwh_per_litre,
        "boiler_efficiency": settings.oil_boiler_efficiency,
        "price_chf_per_litre": settings.oil_price_chf_per_litre,
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
    # Which MPPT input faces which way is in no register - it was read off
    # the daily curve and lives in the .env file (see README). The page needs
    # the names to label the two sides it already has the kWh for.
    payload["pv_string_names"] = list(settings.pv_string_names)
    payload["oil"] = _oil_equivalent()
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


@app.get("/api/finance")
async def finance() -> dict:
    """Where the installation stands against what it cost.

    The return side is measured savings only - no annualisation, and no
    extrapolation backwards over the months before the dashboard existed.
    The single projection is the break-even forecast at the end, and it
    ships with the rate and the number of days that rate rests on, because
    a payback date drawn from eleven weeks of late-summer sun is an
    estimate wearing a calendar's clothes.
    """
    assert store is not None
    totals = store.energy_buckets("all")["totals"]
    buy = settings.price_import_chf_per_kwh
    sell = settings.price_export_chf_per_kwh
    oil = _oil_equivalent()

    tax_benefit = settings.tax_deduction_chf * settings.tax_deduction_rate
    net = settings.investment_chf - settings.subsidy_chf - tax_benefit

    # Each stream is divided by the hours it was actually recorded over,
    # not by the calendar. The clamp on the heat pump went in eleven weeks
    # after the rest of the record started, and dividing its savings by the
    # whole span would report a daily rate the machine never ran at.
    days = (totals["hours"] or 0.0) / 24.0
    hp_days = (totals["heatpump_hours"] or 0.0) / 24.0
    self_consumed = max(0.0, totals["consumption"] - totals["grid_import"])
    hp_kwh = totals["heatpump"] or 0.0
    # Heating's saving is the oil it did not burn, minus the electricity it
    # did use. That electricity is subtracted here and nowhere else: the
    # solar stream prices the kWh the roof supplied whichever load took
    # them, so crediting the pump with free power as well would count the
    # same francs twice.
    heating_chf = hp_kwh * oil["litres_per_kwh"] * oil["price_chf_per_litre"] - hp_kwh * buy

    streams = [
        {
            "key": "pv_self",
            "name": "Solar used in the house",
            "chf": round(self_consumed * buy, 2),
            "days": round(days, 2),
            "basis": "%.1f kWh not bought, at %.2f/kWh" % (self_consumed, buy),
        },
        {
            "key": "pv_export",
            "name": "Solar exported",
            "chf": round((totals["grid_export"] or 0.0) * sell, 2),
            "days": round(days, 2),
            "basis": "%.1f kWh at %.2f/kWh" % (totals["grid_export"] or 0.0, sell),
        },
        {
            "key": "heating",
            "name": "Heating instead of oil",
            "chf": round(heating_chf, 2),
            "days": round(hp_days, 2),
            "basis": "%.1f kWh of compressor, metered for %d of %d recorded hours"
            % (hp_kwh, round(totals["heatpump_hours"] or 0.0), round(totals["hours"] or 0.0)),
        },
    ]
    for stream in streams:
        stream["counted"] = stream["key"] in settings.finance_return_streams
        stream["chf_per_day"] = (
            round(stream["chf"] / stream["days"], 2) if stream["days"] > 0 else None
        )

    counted = [s for s in streams if s["counted"]]
    returned = round(sum(s["chf"] for s in counted), 2)
    rate = sum(s["chf"] / s["days"] for s in counted if s["days"] > 0)
    remaining = round(net - returned, 2)

    # Forecast only when there is an invoice to forecast against and the
    # thing is actually earning. Without those the honest answer is no
    # answer rather than a date at infinity.
    # Days since the installation went live, which is not the same as days
    # recorded: the difference is service downtime, and saying both keeps a
    # rate per recorded day from being read as a rate per day.
    elapsed: float | None = None
    if settings.investment_date:
        try:
            elapsed = round(
                (date.today() - date.fromisoformat(settings.investment_date)).days, 2
            )
        except ValueError:
            elapsed = None

    known = settings.investment_chf > 0
    days_left: int | None = None
    when: date | None = None
    if known and remaining > 0 and rate > 0:
        days_left = ceil(remaining / rate)
        when = date.today() + timedelta(days=days_left)
    elif known and remaining <= 0:
        days_left = 0

    return {
        "currency": "CHF",
        "known": known,
        "investment": {
            "gross": round(settings.investment_chf, 2),
            "subsidy": round(settings.subsidy_chf, 2),
            "tax_deduction": round(settings.tax_deduction_chf, 2),
            "tax_rate": settings.tax_deduction_rate,
            "tax_benefit": round(tax_benefit, 2),
            "net": round(net, 2),
        },
        "measured": {
            "first_record": store.first_timestamp(),
            "earning_since": settings.investment_date or None,
            "recorded_days": round(days, 2),
            "elapsed_days": elapsed,
            "pv_kwh": round(totals["pv"] or 0.0, 1),
            "pv_kwh_per_day": round((totals["pv"] or 0.0) / days, 2) if days > 0 else None,
        },
        "benchmark": _benchmark(days, totals["pv"] or 0.0, returned, buy, sell),
        "streams": streams,
        "returned_chf": returned,
        "remaining_chf": remaining,
        "progress_pct": round(100.0 * returned / net, 1) if net > 0 else None,
        "rate_chf_per_day": round(rate, 2),
        "days_to_break_even": days_left,
        "break_even_date": when.isoformat() if when else None,
    }


@app.get("/api/heatpump/runs")
async def heatpump_runs(days: int = Query(7, ge=1, le=366)) -> dict:
    """Compressor runs against the window the pump is meant to heat in.

    The pump cannot be read or steered from here - its OCI670 gateway
    speaks no Modbus at all - so the clamp meter on its feed is the only
    witness to what its controller decided. This is that record, run by run.
    """
    assert store is not None
    # Whole local days, so a seven-day view is seven calendar rows rather
    # than six and two halves.
    midnight = (
        datetime.now(timezone.utc)
        .astimezone()
        .replace(hour=0, minute=0, second=0, microsecond=0)
    )
    since = (midnight - timedelta(days=days - 1)).astimezone(timezone.utc)
    return {
        "days": days,
        "since": since.isoformat(),
        "oil": _oil_equivalent(),
        **store.heatpump_runs(since.isoformat()),
    }


@app.get("/api/wallbox/sessions")
async def wallbox_sessions(days: int = Query(30, ge=1, le=366)) -> dict:
    assert store is not None
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    sessions = store.wallbox_sessions(since)
    return {
        "days": days,
        "sessions": sessions,
        "total_kwh": round(sum(s["energy_kwh"] for s in sessions), 3),
        # Split by which kind of fact they are: measured off the clamp meter,
        # or reconstructed from the charger's counter before there was one.
        "measured_kwh": round(
            sum(s["energy_kwh"] for s in sessions if s.get("measured")), 3
        ),
        "measured_count": sum(1 for s in sessions if s.get("measured")),
    }


@app.get("/api/wallbox/crosscheck")
async def wallbox_crosscheck(days: int = Query(30, ge=1, le=366)) -> dict:
    """The clamp meter against the charger's own registers.

    Separate from the live payload on purpose: this is a question about the
    record, not about right now, and it is answered by walking the history.
    """
    assert store is not None
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return {"days": days, **store.wallbox_crosscheck(since)}


@app.get("/api/wallbox/events")
async def wallbox_events(
    days: int = Query(30, ge=1, le=366), limit: int = Query(200, ge=1, le=1000)
) -> dict:
    assert store is not None
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return {"days": days, "events": store.wallbox_events(since, limit)}

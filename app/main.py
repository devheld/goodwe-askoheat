from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from calendar import monthrange
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


def _rate_at(day: date) -> tuple[float, float]:
    """The (import, export) tariff in force on a given day.

    Falls back to PRICE_* for any date before the first TARIFFS entry, so
    an installation with no price history still prices its energy.
    """
    rate = (settings.price_import_chf_per_kwh, settings.price_export_chf_per_kwh)
    for year, imp, exp in settings.tariffs:
        if year <= day.year:
            rate = (imp, exp)
        else:
            break
    return rate


def _export_allowed(day: date) -> bool:
    """Whether feed-in was permitted on a given day.

    Until the utility's approval arrives a roof can produce but not sell,
    and surplus with nowhere to go is not revenue anybody failed to earn.
    """
    if not settings.export_allowed_from:
        return True
    try:
        return day >= date.fromisoformat(settings.export_allowed_from)
    except ValueError:
        return True


def _bucket_day(bucket: str) -> date:
    """The day an aggregation bucket falls on.

    Buckets are hours, days or months depending on the period; a month
    becomes its first day, which is where tariff changes land anyway.
    """
    parts = bucket.split("T")[0].split("-")
    while len(parts) < 3:
        parts.append("01")
    return date(int(parts[0]), int(parts[1]), int(parts[2]))


def _priced(buckets: list[dict]) -> dict:
    """Money for a set of buckets, each priced at its own tariff.

    Also returns the hours-weighted average rates for the span, which is
    what a page needs when it wants to print one price next to a figure
    that was earned under several.
    """
    out = {
        "spent": 0.0,
        "earned": 0.0,
        "avoided": 0.0,
        "consumption_value": 0.0,
        "self_consumed_kwh": 0.0,
        "pv_kwh": 0.0,
        "pv_self_chf": 0.0,
        "pv_export_chf": 0.0,
        "heatpump_chf": 0.0,
        # How much of a year this span represents, seasonally weighted:
        # the share of annual yield its months carry, scaled by how much
        # of each month was recorded. Plus the same share priced at the
        # import and export tariffs of the years it falls in, so a quoted
        # annual energy can be turned into money for exactly this span.
        "plan_days": 0.0,
        "plan_year_share": 0.0,
        "plan_import_share": 0.0,
        "plan_export_share": 0.0,
    }
    w_imp = w_exp = weight = 0.0
    seen: set[tuple[float, float]] = set()
    for row in buckets:
        imp, exp = _rate_at(_bucket_day(row["bucket"]))
        seen.add((imp, exp))
        own = max(0.0, (row["consumption"] or 0.0) - (row["grid_import"] or 0.0))
        out["spent"] += (row["grid_import"] or 0.0) * imp
        out["earned"] += (row["grid_export"] or 0.0) * exp
        out["avoided"] += own * imp
        out["consumption_value"] += (row["consumption"] or 0.0) * imp
        out["self_consumed_kwh"] += own
        out["pv_kwh"] += row["pv"] or 0.0
        out["pv_self_chf"] += own * imp
        out["pv_export_chf"] += (row["grid_export"] or 0.0) * exp
        out["heatpump_chf"] += (row["heatpump"] or 0.0) * imp
        hours = row["hours"] or 0.0
        w_imp += imp * hours
        w_exp += exp * hours
        weight += hours
        day_weight = hours / 24.0
        bucket_day = _bucket_day(row["bucket"])
        allowed = _export_allowed(bucket_day)
        # The share of a year's yield this bucket stands for: its month's
        # share of the annual total, times the fraction of that month the
        # record covers. A November bucket is worth a lot fewer kWh than
        # a June one of the same length, and comparing either against a
        # twelfth of a year would be comparing it against the wrong month.
        in_month = monthrange(bucket_day.year, bucket_day.month)[1]
        share = settings.benchmark_monthly_shares[bucket_day.month - 1] * (
            day_weight / in_month
        )
        out["plan_days"] += day_weight
        out["plan_year_share"] += share
        out["plan_import_share"] += imp * share
        out["plan_export_share"] += (exp if allowed else 0.0) * share
    fallback = _rate_at(date.today())
    out["import_per_kwh"] = round(w_imp / weight, 4) if weight > 0 else fallback[0]
    out["export_per_kwh"] = round(w_exp / weight, 4) if weight > 0 else fallback[1]
    out["blended"] = len(seen) > 1
    return out


def _tariff(priced: dict | None = None) -> dict:
    """Today's tariff, or the average over a span that crossed a change."""
    if priced is not None:
        return {
            "currency": "CHF",
            "import_per_kwh": priced["import_per_kwh"],
            "export_per_kwh": priced["export_per_kwh"],
            "blended": priced["blended"],
            "history": [
                {"year": year, "import_per_kwh": imp, "export_per_kwh": exp}
                for year, imp, exp in settings.tariffs
            ],
        }
    imp, exp = _rate_at(date.today())
    return {
        "currency": "CHF",
        "import_per_kwh": imp,
        "export_per_kwh": exp,
        "blended": False,
        "history": [
            {"year": year, "import_per_kwh": i, "export_per_kwh": e}
            for year, i, e in settings.tariffs
        ],
    }


def _benchmark(priced: dict, window: dict, total_days: float) -> dict | None:
    """What the quote said the installation would do, measured against it.

    `window` is the priced span the comparison is allowed to use: the
    months from the day feed-in was permitted. Everything earlier is
    excluded on purpose. Under an export ban the inverter is held to the
    house's own consumption, so the roof was never permitted to show its
    output and the recorded production is a permit, not a measurement. A
    ratio against a predicted annual yield would be meaningless in both
    directions, and the sign of the error is not even knowable.

    Inside the window the quote is used as the straight line it is - a
    fixed annual return from year one - and compared over days actually
    recorded rather than days elapsed, since the quote has no opinion
    about a service outage.

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
    buy = priced["import_per_kwh"]
    sell = priced["export_per_kwh"]
    chf_year_mine = self_kwh * buy + export_kwh * sell
    kwh_day = year_kwh / 365.0

    # Everything from here is the window only - and with no approval date
    # on file there is no window at all. Falling back to the whole record
    # would quietly compare the throttled months after all.
    has_window = bool(settings.export_allowed_from)
    window_days = window["plan_days"] if has_window else 0.0
    pv_kwh = window["pv_kwh"] if has_window else 0.0
    measured_chf = (window["pv_self_chf"] + window["pv_export_chf"]) if has_window else 0.0
    # The quote's annual figures, cut down to this window by season rather
    # than by calendar length, and the money priced at the tariff of the
    # year each month fell in.
    year_share = window["plan_year_share"] if has_window else 0.0
    expected_kwh = year_kwh * year_share
    expected_chf = chf_year * year_share
    expected_chf_mine = (
        (
            self_kwh * window["plan_import_share"]
            + export_kwh * window["plan_export_share"]
        )
        if has_window
        else 0.0
    )
    ready = has_window and window_days >= settings.benchmark_min_days

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
        "window_from": settings.export_allowed_from or None,
        "window_days": round(window_days, 1),
        # What fraction of a year's yield the window's months carry. 0.08
        # over 40 November days says the comparison rests on 8% of a year
        # and not on 11% of one, which is the whole point of the profile.
        "window_year_share": round(year_share, 4),
        "expected_kwh_per_day": (
            round(expected_kwh / window_days, 2) if window_days > 0 else None
        ),
        "excluded_days": round(max(0.0, total_days - window_days), 1),
        "min_days": settings.benchmark_min_days,
        "ready": ready,
        "measured_kwh": round(pv_kwh, 1),
        "measured_kwh_per_day": round(pv_kwh / window_days, 2) if window_days > 0 else None,
        "measured_chf": round(measured_chf, 2),
        "expected_kwh_to_date": round(expected_kwh, 1),
        "expected_chf_to_date": round(expected_chf, 2),
        "expected_chf_to_date_at_my_tariffs": round(expected_chf_mine, 2),
        # What the quote expected from export in a year, which is what the
        # ban made unavailable - at the quote's own export price.
        "export_chf_per_year": round(
            export_kwh * settings.benchmark_export_chf_per_kwh, 2
        ),
        "production_pct": (
            round(100.0 * pv_kwh / expected_kwh, 1) if ready and expected_kwh > 0 else None
        ),
        # Against the plan priced at this household's tariffs, so the ratio
        # is about the roof and the share kept in the house - not about the
        # 15% the two tariff sheets happen to differ by.
        "return_pct": (
            round(100.0 * measured_chf / expected_chf_mine, 1)
            if ready and expected_chf_mine > 0
            else None
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
    # Priced bucket by bucket, each at the tariff that was in force then.
    # Solar that never reached the grid is valued at the import price,
    # because that is exactly the purchase it replaced.
    priced = _priced(payload["buckets"])
    payload["tariff"] = _tariff(priced)
    # Which MPPT input faces which way is in no register - it was read off
    # the daily curve and lives in the .env file (see README). The page needs
    # the names to label the two sides it already has the kWh for.
    payload["pv_string_names"] = list(settings.pv_string_names)
    payload["oil"] = _oil_equivalent()
    payload["costs"] = {
        "spent": round(priced["spent"], 2),
        "earned": round(priced["earned"], 2),
        "avoided": round(priced["avoided"], 2),
        "saved": round(priced["avoided"] + priced["earned"], 2),
        "consumption_value": round(priced["consumption_value"], 2),
        "self_consumed_kwh": round(priced["self_consumed_kwh"], 3),
    }

    # Losses are priced at the import tariff, not the export tariff: with
    # this installation exporting 0.6% of its production, a kWh that was lost
    # or held back is a kWh that had to be bought instead, not one that failed
    # to be sold.
    #
    # At the span's average import price: the loss figures are totals over
    # the whole period and are not broken down per bucket, so there is no
    # per-bucket tariff to apply to them.
    losses = payload["losses"]
    avg_buy = priced["import_per_kwh"]
    losses["real_chf"] = round(losses["real_kwh"] * avg_buy, 2)
    losses["curtailed_chf"] = round(losses["curtailed_kwh"] * avg_buy, 2)
    losses["headroom_chf"] = round(losses["curtailed_split"]["headroom"] * avg_buy, 2)
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
    energy = store.energy_buckets("all")
    totals = energy["totals"]
    # Every month at its own tariff: this counter runs for years and will
    # cross several price changes, and re-pricing history at today's rate
    # would move money that was already earned.
    priced = _priced(energy["buckets"])
    # The benchmark's window: the buckets from the day feed-in was
    # permitted. A month counts only if its first day is inside, which
    # errs toward a shorter window - the safe direction for a comparison.
    window = _priced(
        [r for r in energy["buckets"] if _export_allowed(_bucket_day(r["bucket"]))]
    )
    buy = priced["import_per_kwh"]
    sell = priced["export_per_kwh"]
    oil = _oil_equivalent()

    tax_benefit = settings.tax_deduction_chf * settings.tax_deduction_rate
    gross = settings.investment_chf + settings.investment_extra_chf
    net = gross - settings.subsidy_chf - tax_benefit

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
    heating_chf = (
        hp_kwh * oil["litres_per_kwh"] * oil["price_chf_per_litre"] - priced["heatpump_chf"]
    )

    streams = [
        {
            "key": "pv_self",
            "name": "Solar used in the house",
            "chf": round(priced["pv_self_chf"], 2),
            "days": round(days, 2),
            "basis": "%.1f kWh not bought, at %s%.4f/kWh"
            % (self_consumed, "avg " if priced["blended"] else "", buy),
        },
        {
            "key": "pv_export",
            "name": "Solar exported",
            "chf": round(priced["pv_export_chf"], 2),
            "days": round(days, 2),
            "basis": "%.1f kWh at %s%.4f/kWh"
            % (totals["grid_export"] or 0.0, "avg " if priced["blended"] else "", sell),
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
            "gross": round(gross, 2),
            "invoice": round(settings.investment_chf, 2),
            "extra": round(settings.investment_extra_chf, 2),
            "extra_label": settings.investment_extra_label,
            "subsidy": round(settings.subsidy_chf, 2),
            "tax_deduction": round(settings.tax_deduction_chf, 2),
            "tax_rate": settings.tax_deduction_rate,
            "tax_benefit": round(tax_benefit, 2),
            "net": round(net, 2),
        },
        "tariff": _tariff(priced),
        "measured": {
            "first_record": store.first_timestamp(),
            "earning_since": settings.investment_date or None,
            "recorded_days": round(days, 2),
            "elapsed_days": elapsed,
            "pv_kwh": round(totals["pv"] or 0.0, 1),
            "pv_kwh_per_day": round((totals["pv"] or 0.0) / days, 2) if days > 0 else None,
        },
        "benchmark": _benchmark(priced, window, days),
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

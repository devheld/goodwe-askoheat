from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from calendar import monthrange
from math import ceil
from pathlib import Path

import json

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .askoheat_client import AskoHeatController
from .config import settings
from .controller import SurplusController
from .database import PERIODS, HistoryStore
from .goodwe_client import GoodWeModbusReader
from .mystrom_client import MyStromClient
from .shelly_client import ShellyClient
from .wallbox_client import GoodWeWallboxReader

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
WIDGET_DIR = Path(__file__).resolve().parent / "widget"

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
        main_fuse_a=settings.main_fuse_a,
        main_fuse_warn_pct=settings.main_fuse_warn_pct,
        heatpump_window=(
            settings.heatpump_window_from_h,
            settings.heatpump_window_to_h,
        ),
        heatpump_weekend_window=(
            settings.heatpump_weekend_window_from_h,
            settings.heatpump_weekend_window_to_h,
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
    # The second power plant: a 600 W balcony unit whose inverter is on a
    # myStrom plug. The plug is the only instrument that sees it - it is
    # not on the inverter's MPPT inputs - and setting the host is the
    # statement that the inverter, and not something else, is plugged in.
    balcony = (
        MyStromClient(settings.mystrom_balcony_host, settings.shelly_timeout_s)
        if settings.mystrom_balcony_host
        else None
    )
    controller = SurplusController(
        settings, reader, askoheat, store, wallbox, shelly_meters, balcony
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


def _meter_kw(latest: dict, role: str) -> float | None:
    """A clamp meter's reading, or None when it is not there to ask.

    Clamped at zero: a feed that only draws reads a few watts negative at
    standby, and on a phone that would look like the heat pump exporting.
    """
    meter = (latest.get("shelly") or {}).get(role) or {}
    if not meter.get("online") or meter.get("total_kw") is None:
        return None
    return max(0.0, meter["total_kw"])


@app.get("/api/widget")
async def widget_data() -> dict:
    return _widget_payload()[0]


def _widget_payload() -> tuple[dict, dict]:
    """The handful of numbers the phone widget shows, and nothing else.

    /api/live is several kilobytes of diagnostics; a widget gets a few
    seconds of network on iOS's schedule, so it gets this instead. Today's
    totals come from the same buckets as the energy page.
    """
    if controller is None or controller.latest is None or store is None:
        raise HTTPException(status_code=503, detail="No data received yet")
    latest = controller.latest

    # The charger: its clamp where there is one, because the clamp keeps
    # measuring while the charger itself must not be asked (README 7).
    charger = latest.get("wallbox") or {}
    wallbox_kw = _meter_kw(latest, "wallbox")
    if wallbox_kw is None and charger.get("online"):
        wallbox_kw = charger.get("power_kw")
    balcony = latest.get("balcony") or {}

    today = store.energy_buckets("today")
    totals, metrics = today["totals"], today["metrics"]
    return {
        "timestamp": latest["timestamp"],
        "pv_kw": latest["pv_kw"],
        "balcony_kw": balcony.get("power_kw") if balcony.get("online") else None,
        "consumption_kw": latest["consumption_kw"],
        "grid_import_kw": latest["grid_import_kw"],
        "grid_export_kw": latest["grid_export_kw"],
        "battery_soc_pct": latest["battery_soc_pct"],
        # Positive while charging, negative while discharging.
        "battery_kw": round(
            (latest.get("battery_charge_kw") or 0.0)
            - (latest.get("battery_discharge_kw") or 0.0),
            3,
        ),
        "heatpump_kw": _meter_kw(latest, "heatpump"),
        "wallbox_kw": wallbox_kw,
        "wallbox_vehicle": charger.get("vehicle_connected"),
        "askoheat_w": latest.get("heater_load_w"),
        "askoheat_temp_c": latest.get("askoheat_temp_c"),
        "today": {
            "pv_kwh": totals["pv"],
            "consumption_kwh": totals["consumption"],
            "import_kwh": totals["grid_import"],
            "export_kwh": totals["grid_export"],
            # None, not 0, while no plug has measured today: a dash on the
            # phone rather than a zero nobody measured.
            "balcony_kwh": totals["balcony"] if totals.get("balcony_hours") else None,
            "autarky_pct": metrics["autarky_pct"],
            "self_consumption_pct": metrics["self_consumption_pct"],
        },
    }, today


@app.get("/api/widget/graph")
async def widget_graph() -> dict:
    """The phone widget's numbers plus what its charts need.

    Today hour by hour (local hours, from the energy page's own buckets),
    and the day's battery throughput, which the consumption-origin chart
    splits the day's consumption by.
    """
    payload, today = _widget_payload()
    totals = today["totals"]
    payload["today"]["battery_charge_kwh"] = totals["battery_charge"]
    payload["today"]["battery_discharge_kwh"] = totals["battery_discharge"]
    # Today's consumption by load; "other" is the rest of the house.
    payload["today"]["heatpump_kwh"] = totals["heatpump"]
    payload["today"]["wallbox_kwh"] = totals["wallbox"]
    payload["today"]["askoheat_kwh"] = totals["asko"]
    payload["today"]["other_kwh"] = totals["other"]
    # The battery's charge level at the end of each hour so far: the last
    # sample in it. Readings are stored in UTC; the hours are local.
    now = datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    soc_by_hour: dict[int, float] = {}
    for row in store.history(ceil((now - midnight).total_seconds() / 3600) + 1):
        ts = datetime.fromisoformat(row["timestamp"]).astimezone()
        if ts >= midnight and row.get("battery_soc_pct") is not None:
            soc_by_hour[ts.hour] = row["battery_soc_pct"]
    payload["hours"] = [
        {
            "h": int(b["bucket"][-2:]),
            "pv": b["pv"] or 0.0,
            "consumption": b["consumption"] or 0.0,
            "import": b["grid_import"] or 0.0,
            "export": b["grid_export"] or 0.0,
            "discharge": b["battery_discharge"] or 0.0,
            "soc": soc_by_hour.get(int(b["bucket"][-2:])),
        }
        for b in today["buckets"]
    ]
    return payload


WIDGET_SCRIPTS = {
    # served path: (template, file name the phone saves it as, settings)
    "scriptable": ("scriptable.js", "Energie.js", {}),
    "grafik": ("grafik.js", "Energie Grafik.js", {}),
    # The same charts spread over a large and a medium widget placed one
    # above the other - iOS has no 4x6 size. Its own path, so it updates
    # from its own copy.
    "grafik46": (
        "grafik.js",
        "Energie 4x6.js",
        {
            '/*__LAYOUT__*/"4x4"': '"4x6"',
            '/*__SCRIPT__*/"/widget/grafik.js"': '"/widget/grafik46.js"',
        },
    ),
}


@app.get("/widget/{name}.js")
async def widget_script(name: str, request: Request) -> Response:
    """The Scriptable widget, with this service's addresses filled in.

    The addresses are not in the repository: the one the phone downloaded
    the script from is by definition one it can reach, and WIDGET_HOSTS in
    .env adds the others.

    WIDGET_HOSTS comes first and the requesting address only after it, so
    with WIDGET_HOSTS set the text is the same whichever address asked.
    That matters because the script updates itself by comparing its own
    text with this one: an order that followed the asking address would
    rewrite the phone's copy every time it switched between VPN and LAN.
    """
    if name not in WIDGET_SCRIPTS:
        raise HTTPException(status_code=404, detail="No such widget")
    template, filename, replacements = WIDGET_SCRIPTS[name]
    hosts = [*settings.widget_hosts, str(request.base_url).rstrip("/")]
    hosts = list(dict.fromkeys(hosts))
    code = (WIDGET_DIR / template).read_text(encoding="utf-8").replace(
        "/*__HOSTS__*/[]", json.dumps(hosts)
    )
    for placeholder, value in replacements.items():
        code = code.replace(placeholder, value)
    return Response(
        content=code,
        media_type="text/javascript; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


@app.get("/balcony")
async def balcony_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "balcony.html")


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
    # On the self-consumption figure and the second power plant: `own`
    # below is consumption less import, and both of those already have the
    # balcony netted out of them. The inverter's CT sits at the connection
    # point, upstream of every socket in the house, so a balcony inverter
    # plugged in downstream reduces the measured import and the inverter's
    # computed load by exactly the same amount. The difference - which is
    # what `own` is - is therefore the roof's self-consumption alone,
    # already correct, and adding the balcony to it would be the double
    # count. Export is the exception and is handled in the loop.
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
        # The balcony unit, which earns in both directions and belongs to
        # neither of the two above. See the loop below.
        "balcony_kwh": 0.0,
        "balcony_self_chf": 0.0,
        "balcony_export_chf": 0.0,
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
        # What the balcony made, split by where it went. Both halves are
        # measured per sample in energy_buckets; here they are only priced.
        bal_export = row.get("balcony_export") or 0.0
        bal_self = row.get("balcony_self") or 0.0
        # The roof's export is the house's export less the balcony's share
        # of it. This is the one place the two plants could be confused,
        # and it is a real confusion rather than a tidy one: a kWh the
        # balcony pushed through the meter earns the same rappen whichever
        # panel made it, but it pays off a different purchase.
        roof_export = max(0.0, (row["grid_export"] or 0.0) - bal_export)
        out["spent"] += (row["grid_import"] or 0.0) * imp
        out["earned"] += (row["grid_export"] or 0.0) * exp
        out["avoided"] += own * imp
        out["consumption_value"] += (row["consumption"] or 0.0) * imp
        out["self_consumed_kwh"] += own
        out["pv_kwh"] += row["pv"] or 0.0
        out["pv_self_chf"] += own * imp
        out["pv_export_chf"] += roof_export * exp
        out["heatpump_chf"] += (row["heatpump"] or 0.0) * imp
        out["balcony_kwh"] += row.get("balcony") or 0.0
        out["balcony_self_chf"] += bal_self * imp
        out["balcony_export_chf"] += bal_export * exp
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

    # The second power plant, kept in a block of its own rather than mixed
    # into the totals above. None of it is in pv: the balcony is on a
    # socket, not on an MPPT input, so the inverter never sees it and the
    # two productions are genuinely separate measurements of separate
    # machines. What they are not separate in is the export meter, which
    # is why the roof's export is quoted here with the balcony's share
    # taken off it.
    payload["balcony"] = _balcony_block(totals, priced)
    return payload


def _balcony_block(totals: dict, priced: dict) -> dict | None:
    """The balcony unit's own figures, or None if it was never measured.

    Hidden rather than zeroed when there is no plug: a period that ran
    before the plug was fitted did not have a balcony making nothing, it
    had nobody looking - and the two must not read the same, for the same
    reason the heat pump's clamp carries heatpump_hours.
    """
    hours = totals.get("balcony_hours") or 0.0
    if hours <= 0:
        return None
    made = totals.get("balcony") or 0.0
    exported = totals.get("balcony_export") or 0.0
    used = totals.get("balcony_self") or 0.0
    return {
        "kwh": round(made, 3),
        "self_kwh": round(used, 3),
        "export_kwh": round(exported, 3),
        # The share that stayed in the house. On a 600 W unit against a
        # house that rarely draws less than that, this should sit near
        # 100% - a figure well below it means the house was exporting
        # while the balcony was producing, which is the only condition in
        # which a balcony unit earns the feed-in price instead of the
        # retail one.
        "self_pct": round(100.0 * used / made, 1) if made > 0 else None,
        "chf": round(priced["balcony_self_chf"] + priced["balcony_export_chf"], 2),
        "self_chf": round(priced["balcony_self_chf"], 2),
        "export_chf": round(priced["balcony_export_chf"], 2),
        # How much of the period the plug was actually reporting for, so a
        # month that only has the plug for its last week says so.
        "hours": round(hours, 2),
        "period_hours": round(totals.get("hours") or 0.0, 2),
        "nominal_w": settings.balcony_nominal_w or None,
        "model": settings.balcony_model or None,
        # The roof's export with the balcony's share removed - the one
        # figure on this page that changes because the balcony exists.
        "roof_export_kwh": round(max(0.0, (totals.get("grid_export") or 0.0) - exported), 3),
    }


def _ev_vs_ice(by_day: list[dict], window_days: float) -> dict | None:
    """Kilometres out of kilowatt-hours, and what they would have cost in
    petrol.

    Two conversions and both are stated: how far a kWh goes in the month
    it was charged in (ev_monthly_efficiency, derived from the car's own
    published cold and mild consumption against the local climate
    normals), and what the petrol car it replaces burns per 100 km. The
    kWh themselves are measured; everything after them is arithmetic on
    two settings, which is why the card prints both.

    The petrol side carries no season of its own, although a combustion
    engine also uses more in the cold - a cold start on a short trip
    especially. Leaving that out makes this comparison the conservative
    one, which is the direction to err in.

    Returns None when either conversion is unset: a distance nobody
    measured is worse than no distance at all.
    """
    if settings.ev_km_per_kwh <= 0 or settings.ice_l_per_100km <= 0:
        return None

    # What a kWh at the wallbox is worth in kilometres. The car's own
    # figure counts what left the battery; the clamp counts what entered
    # the house's side of the charger, and the charging loss is the
    # difference. Using the dashboard number against a wall meter would
    # claim about a tenth more distance than was driven.
    at_the_wall = settings.ev_km_per_kwh * (1.0 - settings.ev_charging_loss_pct / 100.0)

    months: dict[str, dict] = {}
    for row in by_day:
        day = _bucket_day(row["day"])
        factor = settings.ev_monthly_efficiency[day.month - 1]
        km = (row["kwh"] or 0.0) * at_the_wall * factor
        litres = km / 100.0 * settings.ice_l_per_100km
        rate, _ = _rate_at(day)
        bucket = months.setdefault(
            day.strftime("%Y-%m"),
            {
                "month": day.strftime("%Y-%m"),
                "km_per_kwh": round(at_the_wall * factor, 2),
                "kwh": 0.0,
                "km": 0.0,
                "litres": 0.0,
                "fuel_chf": 0.0,
                # What the kilometres actually cost: only the share that
                # had to be bought. The rest came off the roof, and the
                # solar stream has already been paid for it.
                "power_chf": 0.0,
            },
        )
        bucket["kwh"] += row["kwh"] or 0.0
        bucket["km"] += km
        bucket["litres"] += litres
        bucket["fuel_chf"] += litres * settings.fuel_chf_per_litre
        bucket["power_chf"] += (row["grid"] or 0.0) * rate

    ordered = [
        {k: (round(v, 2) if isinstance(v, float) else v) for k, v in month.items()}
        for month in sorted(months.values(), key=lambda m: m["month"])
    ]
    fuel = sum(m["fuel_chf"] for m in ordered)
    power = sum(m["power_chf"] for m in ordered)
    measured_kwh = sum(row["kwh"] or 0.0 for row in by_day)
    measured_km = sum(m["km"] for m in ordered)
    grid_share = (
        sum(row["grid"] or 0.0 for row in by_day) / measured_kwh if measured_kwh > 0 else 0.0
    )
    return {
        "km": round(sum(m["km"] for m in ordered), 1),
        "litres": round(sum(m["litres"] for m in ordered), 1),
        "fuel_chf": round(fuel, 2),
        "power_chf": round(power, 2),
        "saved_chf": round(fuel - power, 2),
        "km_per_kwh": settings.ev_km_per_kwh,
        "l_per_100km": settings.ice_l_per_100km,
        "fuel_chf_per_litre": settings.fuel_chf_per_litre,
        "currency": "CHF",
        # A price is not a measurement and this one moved 14% in four
        # months, so it travels with the date it was looked up and the
        # band it has been in.
        "fuel_price_as_of": settings.fuel_price_as_of,
        "fuel_price_range": list(settings.fuel_price_range)
        if settings.fuel_price_range
        else None,
        # The whole year's curve, so the season is visible and not only
        # the two months that happen to be in the record.
        "curve": [round(at_the_wall * f, 2) for f in settings.ev_monthly_efficiency],
        "by_month": ordered,
        "charging_loss_pct": settings.ev_charging_loss_pct,
        "km_per_kwh_at_wall": round(at_the_wall, 2),
        "grid_share": round(grid_share, 4),
        "window_days": round(window_days, 2),
        "km_per_day": round(measured_km / window_days, 1) if window_days > 0 else None,
        "annual": _ev_year_ahead(measured_km, window_days, grid_share, at_the_wall),
    }


def _ev_year_ahead(
    measured_km: float, window_days: float, grid_share: float, at_the_wall: float
) -> dict | None:
    """The next twelve months at the distance measured so far.

    Scaled on kilometres and not on kilowatt-hours, which is the whole
    reason this is worth doing: the same journey needs more energy in
    January than in July, so holding the kWh constant would bake
    whatever weather the window happened to have into every month of the
    year. Holding the kilometres constant is also what the household
    says it does.

    The twelve months are the twelve ahead, not a generic year, so each
    one is priced at the tariff that will actually apply to it.

    Two things are held at their measured value rather than modelled.
    The share that has to be bought stays as it has been - in summer the
    roof will cover more of it, so this errs low - and the petrol side
    has no season at all, although a cold engine also drinks more. Both
    errors point the same way, which is the safe one.
    """
    if window_days < 3 or measured_km <= 0:
        return None
    km_per_day = measured_km / window_days
    first = date.today().replace(day=1)
    rows = []
    for step in range(12):
        month = (first.month - 1 + step) % 12 + 1
        year = first.year + (first.month - 1 + step) // 12
        days_in_month = monthrange(year, month)[1]
        factor = settings.ev_monthly_efficiency[month - 1]
        km = km_per_day * days_in_month
        kwh = km / (at_the_wall * factor)
        litres = km / 100.0 * settings.ice_l_per_100km
        rate, _ = _rate_at(date(year, month, 15))
        fuel_chf = litres * settings.fuel_chf_per_litre
        power_chf = kwh * grid_share * rate
        rows.append(
            {
                "month": "%04d-%02d" % (year, month),
                "km_per_kwh": round(at_the_wall * factor, 2),
                "km": round(km, 0),
                "kwh": round(kwh, 1),
                "litres": round(litres, 1),
                "fuel_chf": round(fuel_chf, 2),
                "power_chf": round(power_chf, 2),
                "saved_chf": round(fuel_chf - power_chf, 2),
            }
        )
    fuel = sum(r["fuel_chf"] for r in rows)
    power = sum(r["power_chf"] for r in rows)
    return {
        "from": rows[0]["month"],
        "to": rows[-1]["month"],
        "km_per_day": round(km_per_day, 1),
        "km": round(sum(r["km"] for r in rows), 0),
        "kwh": round(sum(r["kwh"] for r in rows), 0),
        "litres": round(sum(r["litres"] for r in rows), 0),
        "fuel_chf": round(fuel, 2),
        "power_chf": round(power, 2),
        "saved_chf": round(fuel - power, 2),
        "grid_share": round(grid_share, 4),
        # What the whole projection does if the pump price moves, which
        # it will: the petrol side is litres times a number nobody can
        # forecast, so the sensitivity is quoted rather than hidden.
        "chf_per_10rp": round(sum(r["litres"] for r in rows) * 0.10, 0),
        "saved_low": (
            round(
                sum(r["litres"] for r in rows) * settings.fuel_price_range[0] - power, 0
            )
            if settings.fuel_price_range
            else None
        ),
        "saved_high": (
            round(
                sum(r["litres"] for r in rows) * settings.fuel_price_range[1] - power, 0
            )
            if settings.fuel_price_range
            else None
        ),
        "by_month": rows,
    }


def _payback(key: str, name: str, net: float, members: list[dict]) -> dict:
    """One investment against the savings that actually came from it.

    Each member stream is divided by the days it was measured over, not by
    the calendar, so a stream that started late does not have its rate
    averaged over weeks it did not exist in. A net of zero or less means
    there is nothing to pay off and no date to give.
    """
    returned = round(sum(s["chf"] for s in members), 2)
    rate = sum(s["chf"] / s["days"] for s in members if s["days"] > 0)
    remaining = round(net - returned, 2)
    bases = [s["days"] for s in members if s["days"] > 0]
    days_left: int | None = None
    when: date | None = None
    if net > 0 and remaining <= 0:
        days_left = 0
    elif net > 0 and rate > 0:
        days_left = ceil(remaining / rate)
        when = date.today() + timedelta(days=days_left)
    return {
        "key": key,
        "name": name,
        "net": round(net, 2),
        "returned_chf": returned,
        "remaining_chf": remaining,
        "rate_chf_per_day": round(rate, 2),
        "progress_pct": round(100.0 * returned / net, 1) if net > 0 else None,
        "days_to_break_even": days_left,
        "break_even_date": when.isoformat() if when else None,
        "streams": [s["key"] for s in members],
        # The shortest window any of this group's rates rests on, which is
        # what a reader needs to know before trusting the date.
        "days": round(min(bases), 2) if bases else 0.0,
    }


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
    gross = (
        settings.investment_chf
        + settings.investment_extra_chf
        + settings.balcony_investment_chf
    )
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

    # The car. Every kWh that went into it here is a kWh not bought at a
    # public charger, and the saving is the difference: the public price
    # less the tariff this one was charged at. Whether the roof, the
    # battery or the grid supplied it does not enter into it. A kWh off the
    # roof is already credited in the solar stream at the import tariff, so
    # paying it the full public price here would count those francs twice;
    # a kWh off the grid is in no stream at all and cost the tariff. Both
    # come to the same increment, which is why this is exact without any
    # allocation - the split below is printed because it is worth knowing,
    # not because the money turns on it.
    ev = store.wallbox_energy_split(energy["since"])
    ev_kwh = ev["kwh"]
    ev_chf = 0.0
    if settings.ev_public_chf_per_kwh > 0:
        for year, kwh in ev["by_year"].items():
            year_rate, _ = _rate_at(date(int(year), 7, 1))
            ev_chf += kwh * max(0.0, settings.ev_public_chf_per_kwh - year_rate)
    # A rate drawn from less than a day of charging is noise, not a rate, so
    # under that the stream reports its francs and no projection.
    ev_days = ev["days"] if ev["days"] >= 1.0 else 0.0

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
    if settings.ev_public_chf_per_kwh > 0:
        streams.append(
            {
                "key": "ev_charging",
                "name": "Car charged at home",
                "chf": round(ev_chf, 2),
                "days": round(ev_days, 2),
                "basis": "%.1f kWh into the car%s - %.0f%% straight off the roof, "
                "%.0f%% out of the battery, %.0f%% off the grid; %.2f/kWh saved "
                "against %.2f at a public charger"
                % (
                    ev_kwh,
                    " since %s" % ev["first"][:10] if ev["first"] else "",
                    100.0 * ev["pv_kwh"] / ev_kwh if ev_kwh > 0 else 0.0,
                    100.0 * ev["battery_kwh"] / ev_kwh if ev_kwh > 0 else 0.0,
                    100.0 * ev["grid_kwh"] / ev_kwh if ev_kwh > 0 else 0.0,
                    ev_chf / ev_kwh if ev_kwh > 0 else 0.0,
                    settings.ev_public_chf_per_kwh,
                ),
            }
        )
    # The balcony. Its kilowatt-hours are in no other stream: the roof's
    # two streams are built from the inverter's own pv_kw and from the
    # export it can see, and the balcony is on neither - it is a socket,
    # not an MPPT input. What it displaced is worth the buying price, what
    # it pushed back out is worth the feed-in price, and that second half
    # has just been taken off the roof's export above so the same rappen
    # are not paid twice.
    balcony_kwh = totals["balcony"] or 0.0
    balcony_hours = totals["balcony_hours"] or 0.0
    if balcony_hours > 0:
        bal_self_kwh = totals["balcony_self"] or 0.0
        bal_export_kwh = totals["balcony_export"] or 0.0
        streams.append(
            {
                "key": "balcony",
                "name": "Balcony unit",
                "chf": round(priced["balcony_self_chf"] + priced["balcony_export_chf"], 2),
                # Its own window, like the heat pump's: the plug started
                # measuring long after the roof did, and dividing its
                # francs by the whole record would report a daily rate it
                # never earned.
                "days": round(balcony_hours / 24.0, 2),
                "basis": "%.1f kWh made, %.1f of them used in the house at %s%.4f and "
                "%.1f exported at %s%.4f; metered for %d of %d recorded hours"
                % (
                    balcony_kwh,
                    bal_self_kwh,
                    "avg " if priced["blended"] else "",
                    buy,
                    bal_export_kwh,
                    "avg " if priced["blended"] else "",
                    sell,
                    round(balcony_hours),
                    round(totals["hours"] or 0.0),
                ),
            }
        )

    for stream in streams:
        stream["counted"] = stream["key"] in settings.finance_return_streams
        stream["chf_per_day"] = (
            round(stream["chf"] / stream["days"], 2) if stream["days"] > 0 else None
        )

    counted = [s for s in streams if s["counted"]]
    # Three paybacks: the invoice, whatever was paid outside it, and the
    # two together. The grant and the tax deduction were given for the
    # energy installation, so they come off the invoice's side and not off
    # the charger's.
    extra_members = [s for s in counted if s["key"] in settings.finance_extra_streams]
    balcony_members = [s for s in counted if s["key"] in settings.finance_balcony_streams]
    invoice_members = [
        s
        for s in counted
        if s["key"] not in settings.finance_extra_streams
        and s["key"] not in settings.finance_balcony_streams
    ]
    invoice_net = settings.investment_chf - settings.subsidy_chf - tax_benefit
    groups: list[dict] = []
    if settings.investment_chf > 0 or invoice_members:
        groups.append(_payback("invoice", "PV, battery, heater", invoice_net, invoice_members))
    if settings.investment_extra_chf > 0 or extra_members:
        groups.append(
            _payback(
                "extra",
                settings.investment_extra_label or "Paid outside the invoice",
                settings.investment_extra_chf,
                extra_members,
            )
        )
    if settings.balcony_investment_chf > 0 or balcony_members:
        groups.append(
            _payback(
                "balcony",
                "Balcony unit",
                settings.balcony_investment_chf,
                balcony_members,
            )
        )
    # The sum is worth a row only when there is more than one thing in it.
    if len(groups) > 1:
        groups.append(_payback("all", "Together", net, counted))
    combined = groups[-1] if groups else _payback("all", "Together", net, counted)
    returned = combined["returned_chf"]
    rate = combined["rate_chf_per_day"]
    remaining = combined["remaining_chf"]

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
    # One source of truth for the combined forecast: the group above.
    days_left = combined["days_to_break_even"]
    when = combined["break_even_date"]

    return {
        "currency": "CHF",
        "known": known,
        "investment": {
            "gross": round(gross, 2),
            "invoice": round(settings.investment_chf, 2),
            "extra": round(settings.investment_extra_chf, 2),
            "extra_label": settings.investment_extra_label,
            "balcony": round(settings.balcony_investment_chf, 2),
            "balcony_label": settings.balcony_model or None,
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
        "groups": groups,
        "returned_chf": returned,
        "remaining_chf": remaining,
        "progress_pct": combined["progress_pct"],
        "rate_chf_per_day": round(rate, 2),
        "days_to_break_even": days_left,
        "break_even_date": when,
    }


@app.get("/api/phases")
async def phases(hours: int = Query(24, ge=1, le=24 * 30)) -> dict:
    """Phase current at the house connection, and how close it has come.

    The clamp at the connection point measures the one quantity the
    inverter cannot report, and until now it was written to the database
    and read by nobody.
    """
    assert store is not None
    return store.phase_currents(hours)


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
    sources = store.wallbox_energy_split(since)
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
        # Where the energy came from, over the same window: the page draws
        # it per day and the sessions carry their own share of it. The two
        # prices ride along so the page can say what the mix is worth
        # without fetching the whole finance payload.
        "sources": {
            **sources,
            "public_chf_per_kwh": settings.ev_public_chf_per_kwh,
            "import_chf_per_kwh": _rate_at(date.today())[0],
            "currency": "CHF",
        },
        "ice": _ev_vs_ice(sources["by_day"], sources["days"]),
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

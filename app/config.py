from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    goodwe_host: str
    goodwe_port: int
    goodwe_slave_id: int

    askoheat_host: str
    askoheat_port: int
    askoheat_step_register: int
    askoheat_load_register: int
    askoheat_temp_register: int
    askoheat_slave_id: int
    askoheat_delay_s: int

    # GoodWe AC EV charger (2nd gen). Read-only and entirely optional - the
    # dashboard shows it when it answers and says so when it doesn't. Set
    # WALLBOX_HOST empty to leave it out altogether.
    wallbox_host: str
    wallbox_port: int
    wallbox_slave_id: int
    # Polled on its own, slower clock than the heating loop: the
    # charger has one free Modbus slot and the owner's app needs it
    # too, so this reader takes it briefly and rarely.
    wallbox_poll_interval_s: int
    # Two ways to answer "is a car drawing right now", which is the one
    # question that decides whether the charger may be read. The clamp meter
    # on its feed answers it directly and is used whenever it is there;
    # wallbox_busy_load_kw is the old answer, inferred from the household
    # load, and stays as the fallback for a meter that is away.
    wallbox_meter_busy_kw: float
    wallbox_busy_load_kw: float
    wallbox_idle_ticks: int
    wallbox_idle_poll_interval_s: int
    # Which of the two reading strategies runs. "gated" leaves the charger
    # alone while the household load says a car is drawing; "direct" reads
    # it on the interval whatever the house is doing, which is how this ran
    # before the fault was found. Kept switchable because the fault is the
    # charger's own, so a firmware change can end it - and the only way to
    # find out is to read again and watch whether a charge survives.
    wallbox_poll_mode: str

    # Shelly 3EM clamp meters, HTTP and read-only. Three questions, three
    # devices, each one optional: an empty host leaves that meter out.
    #   grid     - the three main phases. An independent witness to the
    #              connection point and the only instrument here that
    #              measures the current through the main fuse.
    #   heatpump - the heat pump's feed. The largest load in the house and
    #              the one nothing else measures at all.
    #   wallbox  - the charger's feed. This one is not about curiosity: it
    #              makes the charger's own Modbus registers unnecessary
    #              while a car is drawing, which is the whole of section 7.
    # What a device is for is the name it is configured under, not a setting
    # on the device. It decides the sign and the columns - see ShellyClient
    # and Reading in database.py.
    shelly_grid_host: str
    shelly_heatpump_host: str
    shelly_wallbox_host: str
    shelly_port: int
    shelly_timeout_s: float

    # What the heat pump draws flat out, for the chart's y axis. A curve
    # measured against the machine's own ceiling says how hard it is
    # working; one rescaled to its own maximum every time only says what
    # happened to be the busiest minute on screen. 0 turns it off and the
    # axis follows the data again. It never clips: a reading above the
    # ceiling pushes the axis up rather than disappearing.
    heatpump_max_kw: float

    # The hours the pump is meant to heat in, on the local clock, and what
    # counts as its compressor running.
    #
    # Neither is a control. Nothing here can switch this machine: its
    # gateway speaks no Modbus and the controller has no other interface.
    # The window is the line the record gets measured against, so "it ran at
    # three in the morning" becomes a row with a timestamp instead of
    # something somebody heard.
    #
    # 0.5 kW sits in the gap between the three states the clamp actually
    # sees on this machine: the controller alone draws 0.035 kW, a
    # circulation pump on its own 0.13-0.14 kW, and the compressor
    # 1.10-1.21 kW. So it separates "heating" from "idling" and from
    # "circulating", which is the distinction the runs are about.
    heatpump_window_from_h: int
    heatpump_window_to_h: int
    heatpump_run_kw: float

    # The heat pump's electricity expressed in the unit this house used to
    # be heated in. Nothing here is measured: the clamp meter sees
    # electricity going in and never heat coming out, so the conversion is
    # three assumptions, kept in configuration so the page can print them
    # next to the number and a better figure can replace them without
    # touching code.
    #
    # heatpump_spf: heat delivered per kWh of electricity across a season.
    #   The datasheet for this machine (CTA Optiheat 1-5es, brine/water)
    #   gives COP 4.5 at B0/W35, 3.4 at B0/W45 and 2.7 at B0/W55, with
    #   1.1/1.4/1.7 kW drawn - and the clamp reads 1.10-1.21 kW, which is
    #   the B0/W35 column. 4.0 is deliberately below that: over a winter
    #   the brine gets colder and hot water needs a higher flow
    #   temperature, and a lab SCOP is not a seasonal figure.
    # oil_kwh_per_litre: the energy in a litre of heating oil, 10 kWh
    #   (35.9 MJ) by its lower heating value.
    # oil_boiler_efficiency: how much of that an old non-condensing boiler
    #   actually put into the house across a year, flue and standby losses
    #   included. 0.85 is the optimistic end for the oil heating, which is
    #   the fair direction to err in for a comparison it is going to lose.
    heatpump_spf: float
    oil_kwh_per_litre: float
    oil_boiler_efficiency: float
    oil_price_chf_per_litre: float

    # One label per connected MPPT input, in order. How many entries there
    # are is how many inputs the dashboard shows, because how many are
    # wired up is a fact about the roof and not something to infer from a
    # night-time reading of zero. Which roof side an input is on is in no
    # register either: it has to be read off the daily curve, and the side
    # that peaks before noon faces east. Until that is settled the default
    # names claim nothing.
    pv_string_names: tuple[str, ...]

    step_min: int
    step_max: int
    poll_interval_s: int
    zero_threshold_kw: float
    # Hysteresis: a new heating cycle (step 0 -> >0) is only started while the
    # AskoHeat's tank temperature is at or below this. Once heating is
    # already running it is left to the device's own (higher) temperature
    # limit rather than stopped here - see SurplusController._tick().
    heating_start_max_temp_c: float
    # Battery priority: heating only starts once the battery has reached
    # battery_full_soc_pct, and a running heating cycle is stopped again when
    # the SOC falls below battery_keep_heating_soc_pct. The gap between the
    # two is the hysteresis that keeps the heater from switching on and off
    # around a single threshold - the heater's own load is what pushes the
    # SOC back down in the first place.
    battery_full_soc_pct: float
    battery_keep_heating_soc_pct: float
    # Only relevant while the grid connection actually allows export (this
    # installation is at 0% feed-in, see README): with the battery full and
    # power being exported, another heater step is added once at least this
    # much is going to the grid. The AskoHeat's smallest step is ~600W, so a
    # smaller margin would overshoot into grid import.
    surplus_step_margin_kw: float

    # Tariff in CHF per kWh. Display only - the control loop never reads
    # these, it regulates on power, not on price.
    price_import_chf_per_kwh: float
    price_export_chf_per_kwh: float

    # Usable battery capacity in kWh, needed to tell a round-trip loss from
    # energy that is simply still in the battery. Not readable over Modbus
    # here, so it is configuration: derived for this installation from five
    # grid charges that ran from a known SOC to a known SOC (25.6 / 27.1 /
    # 27.5 / 28.3 / 31.8 kWh implied).
    battery_capacity_kwh: float

    db_path: Path


WALLBOX_POLL_MODES = ("gated", "direct")


def _wallbox_poll_mode() -> str:
    """Read WALLBOX_POLL_MODE, falling back to the one that protects a charge.

    A typo must not quietly turn the protection off, so anything
    unrecognised becomes "gated" and says so in the log.
    """
    raw = os.environ.get("WALLBOX_POLL_MODE", "gated").strip().lower()
    if raw in WALLBOX_POLL_MODES:
        return raw
    logging.getLogger(__name__).warning(
        "WALLBOX_POLL_MODE=%r is not one of %s - falling back to 'gated'",
        raw,
        ", ".join(WALLBOX_POLL_MODES),
    )
    return "gated"


PV_STRING_COUNT_DEFAULT = 2


def _pv_string_names() -> tuple[str, ...]:
    """Read PV_STRING_NAMES: one comma-separated label per connected input.

    The count is meaningful, not just the labels - two entries means two
    inputs are wired up, which is what this plant measures (PV1 and PV2
    carry voltage, PV3 and PV4 read 0.0V). An empty setting falls back to
    that count with neutral names, so the dashboard is right about how many
    sides there are before it is right about what they are called.
    """
    given = [n.strip() for n in os.environ.get("PV_STRING_NAMES", "").split(",")]
    given = [n for n in given if n][:4]
    if not given:
        given = ["String %d" % (i + 1) for i in range(PV_STRING_COUNT_DEFAULT)]
    return tuple(given)


def load_settings() -> Settings:
    return Settings(
        goodwe_host=os.environ.get("GOODWE_HOST", "192.168.68.50"),
        goodwe_port=int(os.environ.get("GOODWE_PORT", "502")),
        goodwe_slave_id=int(os.environ.get("GOODWE_SLAVE_ID", "247")),
        askoheat_host=os.environ.get("ASKOHEAT_HOST", "192.168.68.73"),
        askoheat_port=int(os.environ.get("ASKOHEAT_PORT", "502")),
        askoheat_step_register=int(os.environ.get("ASKOHEAT_STEP_REGISTER", "200")),
        askoheat_load_register=int(os.environ.get("ASKOHEAT_LOAD_REGISTER", "110")),
        askoheat_temp_register=int(os.environ.get("ASKOHEAT_TEMP_REGISTER", "325")),
        askoheat_slave_id=int(os.environ.get("ASKOHEAT_SLAVE_ID", "1")),
        askoheat_delay_s=int(os.environ.get("ASKOHEAT_DELAY_S", "10")),
        wallbox_host=os.environ.get("WALLBOX_HOST", "192.168.1.172"),
        wallbox_port=int(os.environ.get("WALLBOX_PORT", "502")),
        wallbox_slave_id=int(os.environ.get("WALLBOX_SLAVE_ID", "247")),
        wallbox_poll_interval_s=int(
            os.environ.get("WALLBOX_POLL_INTERVAL_S", "300")
        ),
        # Power on the charger's own feed that means a car is drawing. The
        # charger's standby was measured here at 4-7W with nothing plugged
        # in, and the smallest charge it can deliver is a single-phase car at
        # the 6A minimum, 1.38kW. 0.1kW sits twenty times above the one and
        # fourteen times below the other, which is the whole reason the clamp
        # replaced the household-load estimate: that estimate had to be set
        # at 3.0kW to stay clear of a kettle, and so could not see a
        # single-phase charge at all.
        wallbox_meter_busy_kw=float(
            os.environ.get("WALLBOX_METER_BUSY_KW", "0.1")
        ),
        # Household load above the heater that means a car is drawing, and
        # therefore that the charger must not be touched. See
        # Controller._poll_wallbox. Above the house's base load (measured up
        # to 2.5kW here) and below a three-phase car at the 6A minimum
        # (4.14kW), so neither a kettle nor a slow charge is misread.
        wallbox_busy_load_kw=float(os.environ.get("WALLBOX_BUSY_LOAD_KW", "3.0")),
        # Ticks the load must stay low before a charge counts as over. A dip
        # of one tick is not the end of one: the longest pause measured here
        # was 2.3 minutes, so five ticks of 70s outlasts it with room to
        # spare, at the price of a first poll delayed by about six minutes.
        wallbox_idle_ticks=int(os.environ.get("WALLBOX_IDLE_TICKS", "5")),
        # Poll interval while a car is plugged in but taking nothing. Every
        # read is a chance to block it from resuming, and a parked car needs
        # no minute-by-minute detail.
        wallbox_idle_poll_interval_s=int(
            os.environ.get("WALLBOX_IDLE_POLL_INTERVAL_S", "1200")
        ),
        wallbox_poll_mode=_wallbox_poll_mode(),
        # SHELLY_HOST is the name the first of these meters had when it was
        # the only one, kept as a fallback so an older .env still works.
        shelly_grid_host=os.environ.get(
            "SHELLY_GRID_HOST", os.environ.get("SHELLY_HOST", "")
        ),
        shelly_heatpump_host=os.environ.get("SHELLY_HEATPUMP_HOST", ""),
        shelly_wallbox_host=os.environ.get("SHELLY_WALLBOX_HOST", ""),
        shelly_port=int(os.environ.get("SHELLY_PORT", "80")),
        # Short on purpose: this is a dashboard reading on the heating loop's
        # clock, and a meter that is slow to answer must not hold up the
        # heater decision behind it.
        shelly_timeout_s=float(os.environ.get("SHELLY_TIMEOUT_S", "3")),
        heatpump_max_kw=float(os.environ.get("HEATPUMP_MAX_KW", "0")),
        heatpump_window_from_h=int(os.environ.get("HEATPUMP_WINDOW_FROM_H", "8")),
        heatpump_window_to_h=int(os.environ.get("HEATPUMP_WINDOW_TO_H", "18")),
        heatpump_run_kw=float(os.environ.get("HEATPUMP_RUN_KW", "0.5")),
        heatpump_spf=float(os.environ.get("HEATPUMP_SPF", "4.0")),
        oil_kwh_per_litre=float(os.environ.get("OIL_KWH_PER_LITRE", "10.0")),
        oil_boiler_efficiency=float(os.environ.get("OIL_BOILER_EFFICIENCY", "0.85")),
        oil_price_chf_per_litre=float(os.environ.get("OIL_PRICE_CHF_PER_LITRE", "1.78")),
        pv_string_names=_pv_string_names(),
        step_min=int(os.environ.get("STEP_MIN", "0")),
        step_max=int(os.environ.get("STEP_MAX", "7")),
        poll_interval_s=int(os.environ.get("POLL_INTERVAL_S", "60")),
        zero_threshold_kw=float(os.environ.get("ZERO_THRESHOLD_KW", "0.01")),
        heating_start_max_temp_c=float(os.environ.get("HEATING_START_MAX_TEMP_C", "55")),
        battery_full_soc_pct=float(os.environ.get("BATTERY_FULL_SOC_PCT", "95")),
        battery_keep_heating_soc_pct=float(
            os.environ.get("BATTERY_KEEP_HEATING_SOC_PCT", "90")
        ),
        surplus_step_margin_kw=float(os.environ.get("SURPLUS_STEP_MARGIN_KW", "0.6")),
        price_import_chf_per_kwh=float(
            os.environ.get("PRICE_IMPORT_CHF_PER_KWH", "0.25")
        ),
        price_export_chf_per_kwh=float(
            os.environ.get("PRICE_EXPORT_CHF_PER_KWH", "0.10")
        ),
        battery_capacity_kwh=float(os.environ.get("BATTERY_CAPACITY_KWH", "27")),
        db_path=PROJECT_ROOT / os.environ.get("DB_PATH", "data/home-energy.db"),
    )


settings = load_settings()

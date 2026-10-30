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
        db_path=PROJECT_ROOT / os.environ.get("DB_PATH", "data/askoheat.db"),
    )


settings = load_settings()

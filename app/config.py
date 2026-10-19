from __future__ import annotations

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

    db_path: Path


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
        db_path=PROJECT_ROOT / os.environ.get("DB_PATH", "data/askoheat.db"),
    )


settings = load_settings()

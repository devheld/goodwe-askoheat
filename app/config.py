from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    sems_account: str
    sems_password: str
    station_id: str

    askoheat_host: str
    askoheat_port: int
    askoheat_step_register: int
    askoheat_load_register: int
    askoheat_temp_register: int
    askoheat_slave_id: int
    askoheat_delay_s: int

    step_min: int
    step_max: int
    poll_interval_s: int
    zero_threshold_kw: float
    battery_discharge_margin: float

    db_path: Path


def load_settings() -> Settings:
    return Settings(
        sems_account=os.environ["SEMS_ACCOUNT"],
        sems_password=os.environ["SEMS_PASSWORD"],
        station_id=os.environ["SEMS_STATION_ID"],
        askoheat_host=os.environ.get("ASKOHEAT_HOST", "192.168.68.73"),
        askoheat_port=int(os.environ.get("ASKOHEAT_PORT", "502")),
        askoheat_step_register=int(os.environ.get("ASKOHEAT_STEP_REGISTER", "200")),
        askoheat_load_register=int(os.environ.get("ASKOHEAT_LOAD_REGISTER", "110")),
        askoheat_temp_register=int(os.environ.get("ASKOHEAT_TEMP_REGISTER", "325")),
        askoheat_slave_id=int(os.environ.get("ASKOHEAT_SLAVE_ID", "1")),
        askoheat_delay_s=int(os.environ.get("ASKOHEAT_DELAY_S", "10")),
        step_min=int(os.environ.get("STEP_MIN", "0")),
        step_max=int(os.environ.get("STEP_MAX", "7")),
        poll_interval_s=int(os.environ.get("POLL_INTERVAL_S", "60")),
        zero_threshold_kw=float(os.environ.get("ZERO_THRESHOLD_KW", "0.01")),
        battery_discharge_margin=float(os.environ.get("BATTERY_DISCHARGE_MARGIN", "0.03")),
        db_path=PROJECT_ROOT / os.environ.get("DB_PATH", "data/askoheat.db"),
    )


settings = load_settings()

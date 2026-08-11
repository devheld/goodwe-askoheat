from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Reading:
    timestamp: str
    pv_kw: float
    consumption_kw: float
    battery_charge_kw: float
    battery_discharge_kw: float
    grid_kw: float
    battery_charging: bool
    step: int
    heater_load_w: int
    askoheat_temp_c: float


class HistoryStore:
    """Stores readings in SQLite for the web interface's history view."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS readings (
                    timestamp TEXT PRIMARY KEY,
                    pv_kw REAL NOT NULL,
                    consumption_kw REAL NOT NULL,
                    battery_charge_kw REAL NOT NULL,
                    battery_discharge_kw REAL NOT NULL,
                    grid_kw REAL NOT NULL,
                    battery_charging INTEGER NOT NULL,
                    step INTEGER NOT NULL,
                    heater_load_w INTEGER NOT NULL,
                    askoheat_temp_c REAL
                )
                """
            )

    def insert(self, reading: Reading) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO readings
                (timestamp, pv_kw, consumption_kw, battery_charge_kw, battery_discharge_kw, grid_kw, battery_charging, step, heater_load_w, askoheat_temp_c)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reading.timestamp,
                    reading.pv_kw,
                    reading.consumption_kw,
                    reading.battery_charge_kw,
                    reading.battery_discharge_kw,
                    reading.grid_kw,
                    int(reading.battery_charging),
                    reading.step,
                    reading.heater_load_w,
                    reading.askoheat_temp_c,
                ),
            )

    def history(self, hours: int) -> list[dict]:
        since_iso = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - hours * 3600, tz=timezone.utc
        ).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM readings WHERE timestamp >= ? ORDER BY timestamp ASC",
                (since_iso,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest(self) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM readings ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

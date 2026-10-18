from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# A sample stands for the time since the previous sample. When the service
# was down that span can be hours, so it is capped: a gap contributes at most
# one poll interval's worth of energy instead of inventing a load that was
# never measured.
MAX_SAMPLE_SPAN_H = 5 / 60

# Aggregation periods offered by /api/energy. fmt is an SQLite strftime
# pattern applied to the LOCAL timestamp, so buckets line up with the days
# and months the user lives in rather than with UTC.
PERIODS: dict[str, dict] = {
    "today": {"fmt": "%Y-%m-%dT%H", "grain": "hour", "label": "Today"},
    "10d": {"fmt": "%Y-%m-%d", "grain": "day", "label": "Last 10 days"},
    "month": {"fmt": "%Y-%m-%d", "grain": "day", "label": "This month"},
    "year": {"fmt": "%Y-%m", "grain": "month", "label": "This year"},
    "all": {"fmt": "%Y-%m", "grain": "month", "label": "All time"},
}


@dataclass(frozen=True)
class Reading:
    timestamp: str
    pv_kw: float
    consumption_kw: float
    battery_charge_kw: float
    battery_discharge_kw: float
    grid_kw: float
    battery_charging: bool
    battery_soc_pct: float | None
    step: int
    heater_load_w: int
    askoheat_temp_c: float
    status: str
    status_text: str
    # The charger is optional hardware and every row recorded before it was
    # wired up keeps NULL in these columns - the energy aggregation reads
    # NULL as "no wallbox load", not as a gap in the data.
    wallbox_power_kw: float | None = None
    wallbox_status_code: int | None = None
    wallbox_charging: bool | None = None
    wallbox_energy_kwh: float | None = None


def period_start(period: str, now: datetime | None = None) -> datetime:
    """Start of an aggregation period in local time, returned as UTC.

    Local, because "today" has to mean the user's day: at 01:00 CEST the UTC
    day started an hour ago, and a UTC cutoff would silently drop the evening
    that just happened.
    """
    local_now = (now or datetime.now(timezone.utc)).astimezone()
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        start = midnight
    elif period == "10d":
        start = midnight - timedelta(days=9)
    elif period == "month":
        start = midnight.replace(day=1)
    elif period == "year":
        start = midnight.replace(month=1, day=1)
    else:
        start = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return start.astimezone(timezone.utc)


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
            # Additive migrations. Every column added after the first release
            # is nullable on purpose: existing rows keep NULL, and each reader
            # decides what that means for it.
            existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(readings)")}
            for column, decl in (
                ("status", "TEXT"),
                ("status_text", "TEXT"),
                ("battery_soc_pct", "REAL"),
                ("wallbox_power_kw", "REAL"),
                ("wallbox_status_code", "INTEGER"),
                ("wallbox_charging", "INTEGER"),
                ("wallbox_energy_kwh", "REAL"),
            ):
                if column not in existing_columns:
                    conn.execute("ALTER TABLE readings ADD COLUMN " + column + " " + decl)

            # State changes of the charger: one row per change rather than one
            # per poll. The readings table already carries per-poll power, so
            # what is worth keeping here is when something actually happened.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wallbox_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status_code INTEGER,
                    status_text TEXT,
                    charge_mode TEXT,
                    power_kw REAL,
                    energy_kwh REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_wallbox_events_ts ON wallbox_events(timestamp)"
            )

    def insert(self, reading: Reading) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO readings
                (timestamp, pv_kw, consumption_kw, battery_charge_kw, battery_discharge_kw,
                 grid_kw, battery_charging, battery_soc_pct, step, heater_load_w,
                 askoheat_temp_c, status, status_text,
                 wallbox_power_kw, wallbox_status_code, wallbox_charging, wallbox_energy_kwh)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reading.timestamp,
                    reading.pv_kw,
                    reading.consumption_kw,
                    reading.battery_charge_kw,
                    reading.battery_discharge_kw,
                    reading.grid_kw,
                    int(reading.battery_charging),
                    reading.battery_soc_pct,
                    reading.step,
                    reading.heater_load_w,
                    reading.askoheat_temp_c,
                    reading.status,
                    reading.status_text,
                    reading.wallbox_power_kw,
                    reading.wallbox_status_code,
                    None if reading.wallbox_charging is None else int(reading.wallbox_charging),
                    reading.wallbox_energy_kwh,
                ),
            )

    def insert_wallbox_event(
        self,
        timestamp: str,
        event: str,
        detail: str,
        status_code: int | None = None,
        status_text: str | None = None,
        charge_mode: str | None = None,
        power_kw: float | None = None,
        energy_kwh: float | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO wallbox_events
                (timestamp, event, detail, status_code, status_text, charge_mode,
                 power_kw, energy_kwh)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    event,
                    detail,
                    status_code,
                    status_text,
                    charge_mode,
                    power_kw,
                    energy_kwh,
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

    def wallbox_events(self, since_iso: str, limit: int = 200) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM wallbox_events
                WHERE timestamp >= ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (since_iso, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def energy_buckets(self, period: str) -> dict:
        """Energy per bucket, integrated from the recorded power samples.

        There is no energy meter behind this: each sample's power is
        multiplied by the time since the previous sample. That is accurate to
        within a poll interval as long as the load does not swing wildly
        between two polls, and it is the only option for the AskoHeat and the
        house load, neither of which exposes a counter.
        """
        spec = PERIODS.get(period) or PERIODS["today"]
        since = period_start(period)
        sql = """
            WITH spans AS (
                SELECT
                    timestamp,
                    pv_kw,
                    consumption_kw,
                    grid_kw,
                    battery_charge_kw,
                    battery_discharge_kw,
                    COALESCE(heater_load_w, 0) / 1000.0 AS asko_kw,
                    COALESCE(wallbox_power_kw, 0) AS wallbox_kw,
                    (julianday(timestamp)
                        - julianday(LAG(timestamp) OVER (ORDER BY timestamp))) * 24.0 AS raw_h
                FROM readings
                WHERE timestamp >= ?
            ),
            capped AS (
                SELECT *, MIN(COALESCE(raw_h, 0.0), ?) AS h FROM spans
            )
            SELECT
                strftime(?, timestamp, 'localtime')                    AS bucket,
                SUM(pv_kw * h)                                         AS pv,
                SUM(consumption_kw * h)                                AS consumption,
                SUM(MAX(grid_kw, 0) * h)                               AS grid_export,
                SUM(MAX(-grid_kw, 0) * h)                              AS grid_import,
                SUM(battery_charge_kw * h)                             AS battery_charge,
                SUM(battery_discharge_kw * h)                          AS battery_discharge,
                SUM(asko_kw * h)                                       AS asko,
                SUM(wallbox_kw * h)                                    AS wallbox,
                SUM(MAX(consumption_kw - asko_kw - wallbox_kw, 0) * h) AS other,
                SUM(h)                                                 AS hours,
                COUNT(*)                                               AS samples
            FROM capped
            GROUP BY bucket
            ORDER BY bucket
        """
        with self._connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    sql, (since.isoformat(), MAX_SAMPLE_SPAN_H, spec["fmt"])
                )
            ]

        keys = (
            "pv",
            "consumption",
            "grid_export",
            "grid_import",
            "battery_charge",
            "battery_discharge",
            "asko",
            "wallbox",
            "other",
            "hours",
        )
        totals = {k: round(sum(r[k] or 0.0 for r in rows), 3) for k in keys}
        for row in rows:
            for k in keys:
                row[k] = round(row[k] or 0.0, 4)

        return {
            "period": period if period in PERIODS else "today",
            "label": spec["label"],
            "grain": spec["grain"],
            "since": since.isoformat(),
            "buckets": rows,
            "totals": totals,
            "metrics": self._metrics(totals),
        }

    @staticmethod
    def _metrics(totals: dict) -> dict:
        consumption = totals["consumption"]
        pv = totals["pv"]
        # Autarky: the share of what the house used that did not come off the
        # grid. Self-consumption: the share of what the roof made that stayed
        # in the house. Both are undefined when the denominator is zero, and
        # None renders as a dash rather than a misleading 0%.
        autarky = (
            round(100.0 * (consumption - totals["grid_import"]) / consumption, 1)
            if consumption > 0
            else None
        )
        self_consumption = (
            round(100.0 * (pv - totals["grid_export"]) / pv, 1) if pv > 0 else None
        )
        return {
            "autarky_pct": None if autarky is None else max(0.0, min(100.0, autarky)),
            "self_consumption_pct": (
                None if self_consumption is None else max(0.0, min(100.0, self_consumption))
            ),
        }

    def wallbox_sessions(self, since_iso: str, gap_minutes: int = 15) -> list[dict]:
        """Contiguous charging runs, rebuilt from the recorded samples.

        A session ends when charging stops or when the samples themselves stop
        for longer than gap_minutes - otherwise a service restart in the middle
        of a charge would read as one long session with a hole in it.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, wallbox_power_kw, wallbox_energy_kwh, wallbox_status_code
                FROM readings
                WHERE timestamp >= ? AND wallbox_charging = 1
                ORDER BY timestamp ASC
                """,
                (since_iso,),
            ).fetchall()

        sessions: list[dict] = []
        current: dict | None = None
        previous_ts: datetime | None = None
        gap = timedelta(minutes=gap_minutes)

        for row in rows:
            ts = datetime.fromisoformat(row["timestamp"])
            power = row["wallbox_power_kw"] or 0.0
            if current is None or previous_ts is None or ts - previous_ts > gap:
                current = {
                    "start": row["timestamp"],
                    "end": row["timestamp"],
                    "energy_kwh": 0.0,
                    "peak_kw": power,
                    "samples": 0,
                }
                sessions.append(current)
            else:
                span_h = min((ts - previous_ts).total_seconds() / 3600.0, MAX_SAMPLE_SPAN_H)
                current["energy_kwh"] += power * span_h
                current["end"] = row["timestamp"]
            current["peak_kw"] = max(current["peak_kw"], power)
            current["samples"] += 1
            previous_ts = ts

        for session in sessions:
            start = datetime.fromisoformat(session["start"])
            end = datetime.fromisoformat(session["end"])
            session["duration_min"] = round((end - start).total_seconds() / 60.0, 1)
            session["energy_kwh"] = round(session["energy_kwh"], 3)
            session["peak_kw"] = round(session["peak_kw"], 2)

        sessions.reverse()
        return sessions

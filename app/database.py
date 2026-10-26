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

# Curtailment is not measured anywhere - nothing in this installation records
# what the roof COULD have produced. It is bounded instead: inside a short
# window in which the battery was full and nothing went to the grid, the plant
# has demonstrated with its own best sample what it was capable of, and every
# sample below that was the inverter throttling production down to the house
# load. Anything the plant held back for the whole window is invisible to this,
# so the result is a lower bound, never a measurement.
#
# The window length is the whole argument. Longer windows use a reference from
# further away in time and grow the number fast (measured here: 64 kWh at 15
# minutes, 97 at 30, 407 at 60, 883 unbounded), so the shortest window that
# still contains a usable reference is the honest choice.
CURTAILMENT_SLOTS_PER_DAY = 96  # 15 minutes
CURTAILMENT_MIN_PV_KW = 0.1
# "Nothing went to the grid" has to tolerate the meter's own noise around zero,
# but stay well under the smallest real export this plant produces.
CURTAILMENT_EXPORT_EPS_KW = 0.05

# A battery figure is only worth showing when most of what went in has come
# back out again. Over a few hours it has not: charging 18kWh and recovering
# 4kWh is a full battery, not an 80% loss, and subtracting the stored part
# leaves a difference far smaller than the error of the capacity estimate.
BATTERY_LOSS_MAX_STORED_SHARE = 0.25

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
    # The Shelly 3EM at the connection point, in the same sign convention as
    # grid_kw above (positive is export). NULL both before it was installed
    # and whenever it did not answer, so a reader comparing it against
    # grid_kw has to skip NULLs rather than read them as zero.
    shelly_grid_kw: float | None = None
    shelly_phase_kw: tuple[float, float, float] | None = None
    # RMS per phase - what the main fuse carries, which no other instrument
    # here measures.
    shelly_currents_a: tuple[float, float, float] | None = None
    shelly_import_kwh: float | None = None
    shelly_export_kwh: float | None = None


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

    def __init__(
        self,
        db_path: Path,
        battery_capacity_kwh: float = 27.0,
        battery_full_soc_pct: float = 95.0,
        heating_start_max_temp_c: float = 52.0,
        step_max: int = 7,
        zero_threshold_kw: float = 0.05,
    ) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        # Measurement parameters, not preferences: the loss buckets cannot be
        # computed without them. Prices stay out of here - those are applied
        # one layer up, in the API.
        self._battery_capacity_kwh = battery_capacity_kwh
        self._battery_full_soc_pct = battery_full_soc_pct
        self._heating_start_max_temp_c = heating_start_max_temp_c
        self._step_max = step_max
        self._zero_threshold_kw = zero_threshold_kw
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
                ("shelly_grid_kw", "REAL"),
                ("shelly_l1_kw", "REAL"),
                ("shelly_l2_kw", "REAL"),
                ("shelly_l3_kw", "REAL"),
                ("shelly_l1_a", "REAL"),
                ("shelly_l2_a", "REAL"),
                ("shelly_l3_a", "REAL"),
                ("shelly_import_kwh", "REAL"),
                ("shelly_export_kwh", "REAL"),
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
                 wallbox_power_kw, wallbox_status_code, wallbox_charging, wallbox_energy_kwh,
                 shelly_grid_kw, shelly_l1_kw, shelly_l2_kw, shelly_l3_kw,
                 shelly_l1_a, shelly_l2_a, shelly_l3_a,
                 shelly_import_kwh, shelly_export_kwh)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    reading.shelly_grid_kw,
                    *(reading.shelly_phase_kw or (None, None, None)),
                    *(reading.shelly_currents_a or (None, None, None)),
                    reading.shelly_import_kwh,
                    reading.shelly_export_kwh,
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
                -- What went into the inverter and never came out of it as
                -- load, storage or export. Clamped per sample because a
                -- negative loss is arithmetic noise, not energy appearing.
                SUM(MAX(pv_kw + battery_discharge_kw + MAX(-grid_kw, 0)
                        - consumption_kw - battery_charge_kw - MAX(grid_kw, 0),
                        0) * h)                                        AS loss_overhead,
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
            "loss_overhead",
            "loss_battery",
            "loss_curtailed",
            "hours",
        )
        losses = self._add_losses(spec["fmt"], since, rows)
        totals = {k: round(sum(r[k] or 0.0 for r in rows), 3) for k in keys}
        for row in rows:
            for k in keys:
                row[k] = round(row[k] or 0.0, 4)
        losses = self._summarise_losses(losses, totals)

        return {
            "period": period if period in PERIODS else "today",
            "label": spec["label"],
            "grain": spec["grain"],
            "since": since.isoformat(),
            "buckets": rows,
            "totals": totals,
            "metrics": self._metrics(totals),
            "losses": losses,
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

    def _add_losses(self, fmt: str, since: datetime, rows: list[dict]) -> dict:
        """Fill loss_battery and loss_curtailed per bucket, return the extras.

        The overhead bucket is already in the main query because it is a
        per-sample quantity. These two are not: the battery needs the SOC at
        each end of a bucket, and curtailment needs a reference drawn from a
        whole window of samples.
        """
        by_bucket = {row["bucket"]: row for row in rows}
        for row in rows:
            row["loss_battery"] = 0.0
            row["loss_curtailed"] = 0.0

        params = {"since": since.isoformat(), "maxh": MAX_SAMPLE_SPAN_H, "fmt": fmt}

        # --------------------------------------------------------- battery
        # Restricted to rows that carry an SOC, so that the throughput and the
        # SOC change describe the same span. Without that restriction the rows
        # recorded before the SOC column existed contribute discharge with no
        # charge to match, and the round trip comes out at 93% for a battery
        # that measures 87%.
        battery_sql = """
            WITH spans AS (
                SELECT strftime(:fmt, timestamp, 'localtime') AS bucket,
                       timestamp, battery_charge_kw, battery_discharge_kw,
                       battery_soc_pct,
                       (julianday(timestamp)
                         - julianday(LAG(timestamp) OVER (ORDER BY timestamp)))
                         * 24.0 AS raw_h
                FROM readings
                WHERE timestamp >= :since
            ),
            valid AS (
                -- The SOC filter sits here, AFTER the window above: a span is
                -- the time since the previous sample, not since the previous
                -- sample that happened to carry an SOC.
                SELECT * FROM spans
                WHERE battery_soc_pct IS NOT NULL AND battery_soc_pct > 0
            ),
            edged AS (
                SELECT *,
                    MIN(COALESCE(raw_h, 0.0), :maxh) AS h,
                    FIRST_VALUE(battery_soc_pct) OVER win AS soc_first,
                    LAST_VALUE(battery_soc_pct) OVER win AS soc_last
                FROM valid
                WINDOW win AS (
                    PARTITION BY bucket ORDER BY timestamp
                    ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                )
            )
            SELECT bucket,
                   SUM(battery_charge_kw * h)    AS charged,
                   SUM(battery_discharge_kw * h) AS drawn,
                   MIN(soc_first)                AS soc_first,
                   MIN(soc_last)                 AS soc_last
            FROM edged
            GROUP BY bucket
            ORDER BY bucket
        """
        charged = drawn = 0.0
        soc_first = soc_last = None
        with self._connect() as conn:
            for row in conn.execute(battery_sql, params):
                stored = (
                    (row["soc_last"] - row["soc_first"]) / 100.0
                    * self._battery_capacity_kwh
                )
                target = by_bucket.get(row["bucket"])
                if target is not None:
                    # Clamped: within one bucket a single step of the integer
                    # SOC reading is worth ~0.27kWh, which can swamp a small
                    # bucket's real loss and push it negative.
                    target["loss_battery"] = max(
                        (row["charged"] or 0.0) - (row["drawn"] or 0.0) - stored, 0.0
                    )
                charged += row["charged"] or 0.0
                drawn += row["drawn"] or 0.0
                if soc_first is None:
                    soc_first = row["soc_first"]
                soc_last = row["soc_last"]

        # ----------------------------------------------------- curtailment
        curtail_sql = """
            WITH spans AS (
                SELECT strftime(:fmt, timestamp, 'localtime') AS bucket,
                       CAST(julianday(timestamp) * :slots AS INTEGER) AS slot,
                       pv_kw, step, askoheat_temp_c, grid_kw,
                       battery_discharge_kw, battery_soc_pct,
                       (julianday(timestamp)
                         - julianday(LAG(timestamp) OVER (ORDER BY timestamp)))
                         * 24.0 AS raw_h
                FROM readings
                WHERE timestamp >= :since
            ),
            throttled AS (
                -- Everything below the window above, so that one isolated
                -- throttled sample weighs its own 70 seconds and not the
                -- whole gap back to the last time the battery was full.
                SELECT *, MIN(COALESCE(raw_h, 0.0), :maxh) AS h
                FROM spans
                WHERE pv_kw > :min_pv
                  AND grid_kw < :export_eps
                  AND battery_discharge_kw < :zero
                  AND battery_soc_pct >= :full
            ),
            reference AS (
                SELECT slot, MAX(pv_kw) AS cap FROM throttled GROUP BY slot
            )
            SELECT t.bucket,
                   SUM((r.cap - t.pv_kw) * t.h) AS curtailed,
                   -- Who could have taken it. The tank already being hot is
                   -- the only one of these the control loop cannot help.
                   SUM(CASE WHEN t.askoheat_temp_c > :hot
                            THEN (r.cap - t.pv_kw) * t.h ELSE 0 END) AS tank_hot,
                   SUM(CASE WHEN t.askoheat_temp_c <= :hot AND t.step >= :step_max
                            THEN (r.cap - t.pv_kw) * t.h ELSE 0 END) AS at_max,
                   SUM(CASE WHEN t.askoheat_temp_c <= :hot
                             AND t.step > 0 AND t.step < :step_max
                            THEN (r.cap - t.pv_kw) * t.h ELSE 0 END) AS headroom,
                   SUM(CASE WHEN t.askoheat_temp_c <= :hot AND t.step = 0
                            THEN (r.cap - t.pv_kw) * t.h ELSE 0 END) AS missed
            FROM throttled t JOIN reference r ON r.slot = t.slot
            GROUP BY t.bucket
            ORDER BY t.bucket
        """
        split = {"tank_hot": 0.0, "at_max": 0.0, "headroom": 0.0, "missed": 0.0}
        with self._connect() as conn:
            cursor = conn.execute(
                curtail_sql,
                {
                    **params,
                    "slots": CURTAILMENT_SLOTS_PER_DAY,
                    "min_pv": CURTAILMENT_MIN_PV_KW,
                    "export_eps": CURTAILMENT_EXPORT_EPS_KW,
                    "zero": self._zero_threshold_kw,
                    "full": self._battery_full_soc_pct,
                    "hot": self._heating_start_max_temp_c,
                    "step_max": self._step_max,
                },
            )
            for row in cursor:
                target = by_bucket.get(row["bucket"])
                if target is not None:
                    target["loss_curtailed"] = row["curtailed"] or 0.0
                for key in split:
                    split[key] += row[key] or 0.0

        return {
            "battery_in_kwh": charged,
            "battery_out_kwh": drawn,
            "battery_stored_kwh": (
                0.0
                if soc_first is None
                else (soc_last - soc_first) / 100.0 * self._battery_capacity_kwh
            ),
            "curtailed_split": split,
        }

    def _summarise_losses(self, extra: dict, totals: dict) -> dict:
        """Turn the per-bucket sums into the figures the tile reports."""
        overhead = totals["loss_overhead"]
        battery = totals["loss_battery"]
        charged = extra["battery_in_kwh"]
        stored = extra["battery_stored_kwh"]
        # Over a short window most of what went in is still in there, and what
        # is left over is smaller than the error on the capacity constant. Say
        # so, rather than print a number that reads as a catastrophic loss.
        battery_usable = (
            charged > 0 and abs(stored) <= BATTERY_LOSS_MAX_STORED_SHARE * charged
        )
        round_trip = (
            round(100.0 * (extra["battery_out_kwh"] + stored) / charged, 1)
            if battery_usable
            else None
        )
        real = overhead + (battery if battery_usable else 0.0)
        pv = totals["pv"]
        curtailed = totals["loss_curtailed"]
        return {
            "overhead_kwh": round(overhead, 2),
            "battery_kwh": round(battery, 2) if battery_usable else None,
            "battery_round_trip_pct": round_trip,
            "battery_in_kwh": round(charged, 2),
            "battery_stored_kwh": round(stored, 2),
            "real_kwh": round(real, 2),
            "real_pct_of_pv": round(100.0 * real / pv, 1) if pv > 0 else None,
            "curtailed_kwh": round(curtailed, 2),
            "curtailed_pct_of_pv": round(100.0 * curtailed / pv, 1) if pv > 0 else None,
            "curtailed_split": {k: round(v, 2) for k, v in extra["curtailed_split"].items()},
            "curtailment_window_min": round(24 * 60 / CURTAILMENT_SLOTS_PER_DAY),
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

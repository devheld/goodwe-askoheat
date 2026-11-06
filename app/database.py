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

# The charger's counter has a resolution of 0.1 kWh, so a reconstructed
# session below that cannot be a charge it recorded - it is a share the
# weighting handed to some other appliance. Dropped rather than listed.
MIN_SESSION_KWH = 0.1

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
    # The same device after it was moved to the heat pump's feed. Its own
    # columns, so that a query on the grid columns above can never pick up a
    # heat pump measurement and both series stay readable across the move.
    # Consumption is positive here. Which set gets written follows
    # SHELLY_ROLE - see SurplusController._tick.
    heatpump_kw: float | None = None
    heatpump_phase_kw: tuple[float, float, float] | None = None
    heatpump_energy_kwh: float | None = None
    # A clamp meter on the charger's feed, which is a different measurement
    # from wallbox_power_kw above: that one is what the charger says about
    # itself, every five minutes and only when it is safe to ask. This one
    # is measured from outside, on every tick, and cannot interrupt a
    # charge. Where the two disagree, this is the witness.
    wallbox_meter_kw: float | None = None
    wallbox_meter_phase_kw: tuple[float, float, float] | None = None
    wallbox_meter_energy_kwh: float | None = None
    # House load per phase, from the inverter rather than from a clamp, so
    # the record of the phase imbalance continues wherever the Shelly ends
    # up - or if it is gone altogether.
    load_phase_kw: tuple[float, float, float] | None = None
    # Production per MPPT input, one roof side each on this plant. Stored
    # because the inverter has no per-input energy counter: the only way to
    # answer "which side produced how much" is to integrate these.
    pv_string_kw: tuple[float, float, float, float] | None = None


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
        wallbox_busy_load_kw: float = 3.0,
        wallbox_idle_ticks: int = 5,
        wallbox_meter_busy_kw: float = 0.1,
        heatpump_run_kw: float = 0.5,
        heatpump_window: tuple[int, int] = (8, 18),
        main_fuse_a: float = 0.0,
        main_fuse_warn_pct: float = 80.0,
    ) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        # Measurement parameters, not preferences: the loss buckets cannot be
        # computed without them. Prices stay out of here - those are applied
        # one layer up, in the API.
        self._battery_capacity_kwh = battery_capacity_kwh
        # The control loop's own definition of "a car is drawing" and its own
        # delay before it believes a charge is over. Reused here to find the
        # run of samples that produced a counter increment - same numbers,
        # same meaning. See _charge_block.
        self._wallbox_busy_load_kw = wallbox_busy_load_kw
        self._wallbox_idle_ticks = wallbox_idle_ticks
        # And for the part of the history that has a clamp meter behind it,
        # where none of that inference is needed: there a charge only has to
        # be told apart from the charger's own standby.
        self._wallbox_meter_busy_kw = wallbox_meter_busy_kw
        # What counts as the heat pump's compressor running, and the hours
        # it is supposed to run in. The window is not a control - nothing
        # can switch this machine - it is only what the runs are compared
        # against. See heatpump_runs.
        self._heatpump_run_kw = heatpump_run_kw
        self._heatpump_window = heatpump_window
        self._main_fuse_a = main_fuse_a
        self._main_fuse_warn_pct = main_fuse_warn_pct
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
                ("heatpump_kw", "REAL"),
                ("heatpump_l1_kw", "REAL"),
                ("heatpump_l2_kw", "REAL"),
                ("heatpump_l3_kw", "REAL"),
                ("heatpump_energy_kwh", "REAL"),
                ("wallbox_meter_kw", "REAL"),
                ("wallbox_meter_l1_kw", "REAL"),
                ("wallbox_meter_l2_kw", "REAL"),
                ("wallbox_meter_l3_kw", "REAL"),
                ("wallbox_meter_energy_kwh", "REAL"),
                ("load_l1_kw", "REAL"),
                ("load_l2_kw", "REAL"),
                ("load_l3_kw", "REAL"),
                ("pv1_kw", "REAL"),
                ("pv2_kw", "REAL"),
                ("pv3_kw", "REAL"),
                ("pv4_kw", "REAL"),
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
                 shelly_import_kwh, shelly_export_kwh,
                 heatpump_kw, heatpump_l1_kw, heatpump_l2_kw, heatpump_l3_kw,
                 heatpump_energy_kwh, load_l1_kw, load_l2_kw, load_l3_kw,
                 wallbox_meter_kw, wallbox_meter_l1_kw, wallbox_meter_l2_kw,
                 wallbox_meter_l3_kw, wallbox_meter_energy_kwh,
                 pv1_kw, pv2_kw, pv3_kw, pv4_kw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?)
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
                    reading.heatpump_kw,
                    *(reading.heatpump_phase_kw or (None, None, None)),
                    reading.heatpump_energy_kwh,
                    *(reading.load_phase_kw or (None, None, None)),
                    reading.wallbox_meter_kw,
                    *(reading.wallbox_meter_phase_kw or (None, None, None)),
                    reading.wallbox_meter_energy_kwh,
                    *(reading.pv_string_kw or (None, None, None, None)),
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

    def first_timestamp(self) -> str | None:
        """The oldest sample there is - the day the record starts, which is
        not necessarily the day the installation started earning."""
        with self._connect() as conn:
            row = conn.execute("SELECT MIN(timestamp) AS t FROM readings").fetchone()
        return row["t"] if row and row["t"] else None

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

        There is no energy meter behind most of this: each sample's power is
        multiplied by the time since the previous sample. That is accurate to
        within a poll interval as long as the load does not swing wildly
        between two polls, and it is the only option for the AskoHeat and the
        house load, neither of which exposes a counter.

        The charger used to be the exception, because integrating its own
        power samples was wrong in both directions and its counter was the
        only trustworthy number it had. Since 31 Oct 2026 it is the
        best-measured load in the house: a clamp meter on its feed, read
        every tick like any other. The counter remains behind the history
        from before that. See _wallbox_series.
        """
        spec = PERIODS.get(period) or PERIODS["today"]
        since = period_start(period)
        sql = """
            WITH spans AS (
                SELECT
                    timestamp,
                    pv_kw,
                    pv1_kw, pv2_kw, pv3_kw, pv4_kw,
                    consumption_kw,
                    grid_kw,
                    battery_charge_kw,
                    battery_discharge_kw,
                    COALESCE(heater_load_w, 0) / 1000.0 AS asko_kw,
                    heatpump_kw,
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
                -- One MPPT input per roof side. COALESCE rather than a plain
                -- sum because these columns only exist from the day they were
                -- added, and a row without them must not pull the whole bucket
                -- to NULL. pv_split is the production that does have a split,
                -- so the page can show the rest as unrecorded instead of
                -- letting the two sides quietly stand in for the total.
                SUM(COALESCE(pv1_kw, 0) * h)                           AS pv1,
                SUM(COALESCE(pv2_kw, 0) * h)                           AS pv2,
                SUM(COALESCE(pv3_kw, 0) * h)                           AS pv3,
                SUM(COALESCE(pv4_kw, 0) * h)                           AS pv4,
                SUM(CASE WHEN pv1_kw IS NULL THEN 0
                         ELSE pv_kw * h END)                           AS pv_split,
                SUM(consumption_kw * h)                                AS consumption,
                SUM(MAX(grid_kw, 0) * h)                               AS grid_export,
                SUM(MAX(-grid_kw, 0) * h)                              AS grid_import,
                SUM(battery_charge_kw * h)                             AS battery_charge,
                SUM(battery_discharge_kw * h)                          AS battery_discharge,
                SUM(asko_kw * h)                                       AS asko,
                -- The heat pump has a clamp meter on its feed. COALESCE
                -- because the column only exists from the day it was
                -- fitted; heatpump_hours says how much of the period that
                -- covers, so the page can admit the rest is still inside
                -- the household figure rather than implying the pump was
                -- off.
                SUM(COALESCE(heatpump_kw, 0) * h)                      AS heatpump,
                SUM(CASE WHEN heatpump_kw IS NULL THEN 0 ELSE h END)   AS heatpump_hours,
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
            "pv1",
            "pv2",
            "pv3",
            "pv4",
            "pv_split",
            "consumption",
            "grid_export",
            "grid_import",
            "battery_charge",
            "battery_discharge",
            "asko",
            "heatpump",
            "heatpump_hours",
            "wallbox",
            "wallbox_measured",
            "other",
            "loss_overhead",
            "loss_battery",
            "loss_curtailed",
            "hours",
        )
        # Not in the SQL above because the charger's series spans two eras
        # and needs Python to walk it: measured from its clamp meter where
        # there is one, reconstructed from its counter before that (see
        # _wallbox_series). The rest of the house is then what is left once
        # the heater, the pump and the car are taken off the measured
        # consumption.
        wallbox, wallbox_measured = self._wallbox_energy_by_bucket(spec["fmt"], since)
        for row in rows:
            row["wallbox"] = wallbox.get(row["bucket"], 0.0)
            row["wallbox_measured"] = wallbox_measured.get(row["bucket"], 0.0)
            row["other"] = max(
                (row["consumption"] or 0.0)
                - (row["asko"] or 0.0)
                - (row["heatpump"] or 0.0)
                - row["wallbox"],
                0.0,
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

    def _wallbox_series(self, since_iso: str, fmt: str = "%Y-%m-%d") -> list[dict]:
        """Per-sample charger energy. Measured where there is a meter,
        reconstructed where there is not.

        Two eras, and every sample says which one it is in.

        **From 31 Oct 2026**: a Shelly 3EM clamp sits on the charger's feed
        and wallbox_meter_kw is an ordinary measurement of the car, taken on
        every tick whatever the charger is doing. It is integrated like any
        other power column and nothing below it applies - no counter, no
        weighting, no inference. The charger's own counter is deliberately
        dropped from the arithmetic for these samples rather than averaged
        in: the two describe the same energy, and the one that has to be
        guessed at must not get a second vote.

        **Before that**, the only numbers were the charger's own, and its
        recorded power cannot be integrated. The box is polled on a slow
        clock and not at all while a car is drawing (README section 7), so
        one five-minute snapshot got repeated into every 70s row: across ten
        days that overstated the energy by a factor of 2.5 against the
        charger's own counter. Since the poll gate exists the samples read 0
        throughout a charge instead, which is how two days of charging came
        to show up as nothing at all.

        Register 10065 is cumulative and exact - 78.1 kWh before a charge,
        88.0 kWh after, with no read in between - so the counter set the
        scale. What it could not say is when the energy flowed, because it is
        only ever observed after the fact. That came from the only other
        witness, the house load above the heater: on 27 Oct 2026 it tracked a
        one-hour charge at 11 kW to within 11% of the counter's 9.9 kWh. So
        each increment is spread over the samples since the previous
        observation. A span where the counter did not move contributes
        nothing, which also means charging from before the counter worked (it
        read a flat 0.0 until 23 Oct 2026) is absent rather than guessed at.

        That machinery stays because the history it explains is still in the
        database. It applies to no new sample.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT timestamp,
                       strftime(?, timestamp, 'localtime') AS bucket,
                       MAX(consumption_kw
                           - MAX(COALESCE(heater_load_w, 0), 0) / 1000.0, 0) AS house_kw,
                       consumption_kw,
                       grid_kw,
                       wallbox_meter_kw,
                       wallbox_energy_kwh
                FROM readings
                WHERE timestamp >= ?
                ORDER BY timestamp ASC
                """,
                (fmt, since_iso),
            ).fetchall()

        series: list[dict] = []
        span_start = 0
        previous_counter: float | None = None
        previous_ts: datetime | None = None

        for row in rows:
            ts = datetime.fromisoformat(row["timestamp"])
            hours = (
                0.0
                if previous_ts is None
                else min(
                    (ts - previous_ts).total_seconds() / 3600.0,
                    MAX_SAMPLE_SPAN_H,
                )
            )
            # How the house as a whole was being supplied at this
            # moment, which is the share this sample's charging is
            # attributed by. Pro rata and not marginal: the solar stream
            # credits every self-consumed kWh at the import tariff
            # whichever load took it, and the car is not a special case.
            # The split decides which stream carries a kWh, never the
            # total - see ev_public_chf_per_kwh.
            house = row["consumption_kw"] or 0.0
            imported = max(0.0, -(row["grid_kw"] or 0.0))
            grid_share = min(1.0, imported / house) if house > 0 else 0.0
            metered = row["wallbox_meter_kw"] is not None
            # A negative reading on a load means a clamp on backwards, not a
            # car exporting, so it contributes nothing rather than
            # subtracting energy from the day.
            measured_kw = max(row["wallbox_meter_kw"], 0.0) if metered else 0.0
            series.append(
                {
                    "timestamp": row["timestamp"],
                    "bucket": row["bucket"],
                    "house_kw": row["house_kw"] or 0.0,
                    "hours": hours,
                    # Which era this sample is in, carried all the way out to
                    # the session list: a measured session and a
                    # reconstructed one are not the same kind of fact and the
                    # page says so.
                    "metered": metered,
                    # The share of this sample that came off the grid
                    # rather than out of the roof or the battery.
                    "grid_share": grid_share,
                    "kwh": measured_kw * hours,
                    "kw": measured_kw,
                }
            )
            previous_ts = ts

            if metered:
                # Measured, so there is nothing to reconstruct. Re-basing the
                # counter here also means an increment observed after a gap
                # in the meter's coverage is dropped instead of being spread
                # backwards over samples that already carry their own
                # measurement.
                previous_counter = None
                span_start = len(series)
                continue

            counter = row["wallbox_energy_kwh"]
            if counter is None:
                continue
            if previous_counter is None or counter < previous_counter:
                # First value, or the counter went backwards - a reset, or a
                # misread. Nothing to attribute either way, so start fresh.
                previous_counter = counter
                span_start = len(series)
                continue
            if counter == previous_counter:
                # Not an observation anything can be concluded from. This
                # column carries the last snapshot on EVERY row, including
                # all the rows where the charger was deliberately not read,
                # so an unchanged value does not mean no energy flowed. Only
                # a change is a fact.
                continue
            self._spread(series[span_start:], counter - previous_counter)
            previous_counter = counter
            # The increment is accounted for, so whatever the next one covers
            # happened after this row.
            span_start = len(series)

        return series

    def _charge_block(
        self, span: list[dict], floor: float, needed_kwh: float
    ) -> list[dict]:
        """The run of samples that produced one counter increment.

        Found by walking backwards from the read that observed it, because
        the gate's timing says the charge ended just before that read: the
        charger is only read again once the house has been quiet for
        wallbox_idle_ticks, so the observation follows the last charging
        sample by that many ticks and no more. On both charges of 26 and 27
        Oct 2026 the quiet tail before the observing read was five samples
        long on the nose - the gate's own signature, and the reason this can
        be read backwards at all.

        A sample counts as charging while the house draws at least
        wallbox_busy_load_kw above the heater, the same test the gate applies
        live. Short dips stay inside the run: a dip the gate would have sat
        out cannot have ended the charge, or the charger would have been read
        and the counter would have moved there instead.

        Keeping the walk this tight is what keeps a kettle out of a charge.
        Weighting every sample over the threshold anywhere in the span put
        0.53 kWh on a 3.5 kW household load three hours before the car was
        even plugged in.

        A longer quiet run only ends the walk once the block can actually
        carry the increment. If the counter reports more energy than the
        samples found so far can account for, the block is incomplete by
        definition and the walk goes on - which is what the charging before
        the gate existed looks like, a burst every few minutes with pauses
        between them that the counter did not get read in. Without this the
        arithmetic still balanced but the derived power did not: a block
        holding a tenth of its increment claimed 102 kW from an 11 kW box.
        """
        block: list[dict] = []
        covered = 0.0
        quiet = 0
        for sample in reversed(span):
            if sample["house_kw"] >= self._wallbox_busy_load_kw:
                quiet = 0
                block.append(sample)
                covered += (sample["house_kw"] - floor) * sample["hours"]
            else:
                quiet += 1
                if quiet > self._wallbox_idle_ticks and covered >= needed_kwh:
                    break
            # hours is the step from the previous sample to this one, capped,
            # so a capped value means the recording stopped there. Whatever
            # came before is on the far side of a hole.
            if sample["hours"] >= MAX_SAMPLE_SPAN_H:
                break
        block.reverse()
        return block

    def _spread(self, span: list[dict], delta_kwh: float) -> None:
        """Place one counter increment inside the span it accumulated over.

        Where - which samples - comes from _charge_block. How much of it each
        of them gets is the load above the span's median, so the busiest part
        of a charge carries the most of it.

        The median rather than the minimum because a car is the exception in
        any span long enough to matter, so the typical sample is the house
        without it - and because the minimum is not robust: the heater's load
        is read a moment after the inverter's, so a step change leaves single
        samples where the subtraction lands near zero, and one of those as the
        floor lifts the whole day over the threshold.

        An appliance over the threshold inside the charge itself still takes a
        share it did not earn. Nothing here can tell a 3 kW oven from 3 kW of
        car, and the charger is not going to say - but it now has to be
        running during the charge to get anything at all.
        """
        if not span:
            return
        floors = sorted(sample["house_kw"] for sample in span)
        floor = floors[len(floors) // 2]
        block = self._charge_block(span, floor, delta_kwh)
        weights = [max(sample["house_kw"] - floor, 0.0) * sample["hours"] for sample in block]
        total = sum(weights)
        if total <= 0:
            # Nothing before the read looks like a car: a charge shorter than
            # the gap between two reads, or a span of a single sample. Put the
            # energy where it was seen rather than nowhere - but leave kw at
            # zero, because dividing a whole charge by one 70s sample would
            # claim a power of several hundred kW.
            span[-1]["kwh"] += delta_kwh
            return
        for sample, weight in zip(block, weights):
            if weight:
                sample["kwh"] += delta_kwh * weight / total
                # The car cannot have drawn more than the house drew above
                # its own base load, so the quotient is capped there. It
                # needs to be: where the service was down for part of a
                # charge, the surviving samples have to carry the whole
                # increment, and dividing it by them claimed 102 kW from an
                # 11 kW box. The energy is still placed in full - only the
                # power stops pretending to be measured.
                sample["kw"] = min(
                    sample["kwh"] / sample["hours"] if sample["hours"] else 0.0,
                    sample["house_kw"] - floor,
                )

    def _wallbox_energy_by_bucket(
        self, fmt: str, since: datetime
    ) -> tuple[dict[str, float], dict[str, float]]:
        """Charger energy per bucket, and how much of it was measured.

        Two dicts rather than one, because a bucket that mixes a measurement
        with a reconstruction has to be able to say so.
        """
        buckets: dict[str, float] = {}
        measured: dict[str, float] = {}
        for sample in self._wallbox_series(since.isoformat(), fmt):
            if not sample["kwh"]:
                continue
            bucket = sample["bucket"]
            buckets[bucket] = buckets.get(bucket, 0.0) + sample["kwh"]
            if sample["metered"]:
                measured[bucket] = measured.get(bucket, 0.0) + sample["kwh"]
        return buckets, measured

    def wallbox_energy_split(self, since_iso: str) -> dict:
        """Energy into the car, per year, and how it was supplied.

        Per calendar year because the money it stands for is priced at the
        year's tariff. The own/grid split is pro rata on the house's supply
        at each sample (see _wallbox_series); it is information about the
        installation and not an input to the saving, which comes out the
        same either way.

        ``first`` is the first sample carrying any charging at all, and
        ``days`` runs from there to the end of the record rather than over
        the whole history: the car arrived two months after the recording
        started, and a rate per recorded day would otherwise be averaged
        over weeks when there was nothing to charge. Days since the first
        charge with no charging in them are real, though, so they stay in.
        """
        by_year: dict[str, float] = {}
        total = metered = own = grid = 0.0
        first: str | None = None
        last: str | None = None
        for sample in self._wallbox_series(since_iso):
            last = sample["timestamp"]
            kwh = sample["kwh"]
            if not kwh:
                continue
            total += kwh
            if sample["metered"]:
                metered += kwh
            grid += kwh * sample["grid_share"]
            own += kwh * (1.0 - sample["grid_share"])
            year = sample["timestamp"][:4]
            by_year[year] = by_year.get(year, 0.0) + kwh
            if first is None:
                first = sample["timestamp"]

        days = 0.0
        if first and last:
            days = max(
                0.0,
                (
                    datetime.fromisoformat(last) - datetime.fromisoformat(first)
                ).total_seconds()
                / 86400.0,
            )
        return {
            "kwh": round(total, 3),
            "metered_kwh": round(metered, 3),
            "own_kwh": round(own, 3),
            "grid_kwh": round(grid, 3),
            "by_year": {y: round(v, 3) for y, v in sorted(by_year.items())},
            "first": first,
            "days": round(days, 3),
        }

    def wallbox_crosscheck(self, since_iso: str) -> dict:
        """Do the charger and the clamp on its feed agree?

        Three independent witnesses to the same energy, and this compares
        what can be compared:

        * **power** - the clamp against register 10015, on samples where both
          were recorded. Only ever while no car is drawing, because that is
          the only time the charger is read at all, and the register is
          quantised to 0.1 kW, which is twenty times coarser than the clamp.
          So agreement here pins the zero point and nothing else.
        * **energy** - the clamp's own counter against register 10065, both
          cumulative, differenced over the window. This is the real check and
          it needs a charge to say anything: the register's resolution is 0.1
          kWh, so standby alone will never move it.
        * **standby** - what the charger draws with nothing plugged in. The
          clamp can see it; the register cannot resolve it at all.

        Nothing is judged here. The numbers come out and the page says what
        they do and do not establish.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, wallbox_meter_kw, wallbox_power_kw,
                       wallbox_meter_energy_kwh, wallbox_energy_kwh
                FROM readings
                WHERE timestamp >= ? AND wallbox_meter_kw IS NOT NULL
                ORDER BY timestamp ASC
                """,
                (since_iso,),
            ).fetchall()

        diffs: list[float] = []
        standby: list[float] = []
        meter_counter: list[tuple[str, float]] = []
        charger_counter: list[tuple[str, float]] = []

        for row in rows:
            clamp = row["wallbox_meter_kw"]
            register = row["wallbox_power_kw"]
            if register is not None:
                diffs.append(clamp - register)
                # "Nothing plugged in" is the charger's own word for it
                # (status 0), not an inference from the power being small.
                if (register or 0) == 0:
                    standby.append(clamp)
            if row["wallbox_meter_energy_kwh"] is not None:
                meter_counter.append((row["timestamp"], row["wallbox_meter_energy_kwh"]))
            if row["wallbox_energy_kwh"] is not None:
                charger_counter.append((row["timestamp"], row["wallbox_energy_kwh"]))

        def span(counter: list[tuple[str, float]]) -> dict | None:
            if len(counter) < 2:
                return None
            first, last = counter[0], counter[-1]
            hours = (
                datetime.fromisoformat(last[0]) - datetime.fromisoformat(first[0])
            ).total_seconds() / 3600.0
            return {
                "from": first[0],
                "to": last[0],
                "hours": round(hours, 2),
                "kwh": round(last[1] - first[1], 3),
            }

        return {
            "metered_samples": len(rows),
            "metered_from": rows[0]["timestamp"] if rows else None,
            # Where both were recorded on the same tick.
            "power_samples": len(diffs),
            "power_mean_diff_kw": (
                round(sum(diffs) / len(diffs), 4) if diffs else None
            ),
            "power_max_diff_kw": (
                round(max(diffs, key=abs), 4) if diffs else None
            ),
            "register_resolution_kw": 0.1,
            "register_resolution_kwh": 0.1,
            "standby_kw": (
                round(sorted(standby)[len(standby) // 2], 4) if standby else None
            ),
            "standby_samples": len(standby),
            "meter_counter": span(meter_counter),
            "charger_counter": span(charger_counter),
        }

    def phase_currents(self, hours: int = 24) -> dict:
        """Current per phase at the house connection: now, and the peaks.

        The peaks are the point, not the live value. A fuse is not tripped
        by an average, and nobody is looking at a dashboard at the moment
        it would matter - this installation had already reached 22 A on one
        phase at half past nine at night, and a live gauge would have shown
        it to an empty room. So each phase carries what it is drawing now,
        the worst it reached inside the window, and the worst it has ever
        reached, and the card leads with the last of those.

        Aggregated in SQL rather than in Python: this table grows by a row
        a minute forever, and three MAX() calls do not need 500,000 rows
        carried into the process to find one number.
        """
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        cols = ("shelly_l1_a", "shelly_l2_a", "shelly_l3_a")
        maxes = ", ".join("MAX(%s) AS %s" % (c, c) for c in cols)
        with self._connect() as conn:
            now = conn.execute(
                "SELECT timestamp, %s FROM readings WHERE shelly_l1_a IS NOT NULL "
                "ORDER BY timestamp DESC LIMIT 1" % ", ".join(cols)
            ).fetchone()
            window = conn.execute(
                "SELECT %s, COUNT(shelly_l1_a) AS n FROM readings WHERE timestamp >= ?"
                % maxes,
                (since,),
            ).fetchone()
            ever = conn.execute("SELECT %s FROM readings" % maxes).fetchone()

            phases = []
            for index, col in enumerate(cols, 1):
                phases.append(
                    {
                        "name": "L%d" % index,
                        "now_a": round(now[col], 2) if now and now[col] is not None else None,
                        "peak_a": round(window[col], 2) if window[col] is not None else None,
                        "ever_a": round(ever[col], 2) if ever[col] is not None else None,
                    }
                )

            # When the worst of all three happened. Equality on a REAL is
            # safe here because the value came out of that same column.
            worst = None
            ranked = [p for p in phases if p["ever_a"] is not None]
            if ranked:
                top = max(ranked, key=lambda p: p["ever_a"])
                col = cols[int(top["name"][1:]) - 1]
                row = conn.execute(
                    "SELECT timestamp FROM readings WHERE %s = ? "
                    "ORDER BY timestamp DESC LIMIT 1" % col,
                    (top["ever_a"],),
                ).fetchone()
                worst = {
                    "name": top["name"],
                    "amps": top["ever_a"],
                    "at": row["timestamp"] if row else None,
                }

        return {
            "fuse_a": self._main_fuse_a,
            "warn_pct": self._main_fuse_warn_pct,
            "hours": hours,
            "now_at": now["timestamp"] if now else None,
            "samples": (window["n"] if window else 0) or 0,
            "phases": phases,
            "worst_ever": worst,
        }

    def heatpump_runs(self, since_iso: str, gap_minutes: int = 10) -> dict:
        """Compressor runs of the heat pump, from the clamp meter on its feed.

        A run is a maximal series of samples at or above the compressor
        threshold. A break longer than gap_minutes starts a new one, so a
        service restart in the middle of a run does not join two of them
        into one that covers the outage.

        Both ends are known to within one poll interval - the compressor
        started somewhere between the last quiet sample and the first loud
        one - which is 70 seconds against runs that last an hour.

        Energy is power times the interval since the previous sample, capped
        per sample like every other energy figure here, rather than the
        clamp's own counter. The counter is the better integral, but it
        restarts with the device, and a run that straddled a restart would
        read as the entire lifetime of the meter.

        Hours are local, because the window the pump is meant to heat in is
        a wall clock and so is the night somebody sleeps through.
        """
        from_h, to_h = self._heatpump_window
        sql = """
            SELECT timestamp, heatpump_kw,
                   (julianday(timestamp)
                       - julianday(LAG(timestamp) OVER (ORDER BY timestamp)))
                       * 24.0 AS raw_h
            FROM readings
            WHERE timestamp >= ? AND heatpump_kw IS NOT NULL
            ORDER BY timestamp
        """
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, (since_iso,))]

        runs: list[dict] = []
        current: dict | None = None
        previous_ts: datetime | None = None
        gap = timedelta(minutes=gap_minutes)
        measured_h = 0.0
        standby_kwh = 0.0

        for row in rows:
            ts = datetime.fromisoformat(row["timestamp"])
            hours = min(row["raw_h"] or 0.0, MAX_SAMPLE_SPAN_H)
            kw = row["heatpump_kw"]
            kwh = kw * hours
            measured_h += hours
            # Local hour, because that is what the time program is set in.
            local = ts.astimezone()
            inside = from_h <= local.hour < to_h

            if kw < self._heatpump_run_kw:
                # Everything the machine draws while not compressing: the
                # controller around the clock plus a circulation pump at the
                # edges of a run. Reported once, as the floor the runs sit
                # on, rather than as runs of its own.
                standby_kwh += kwh
                current = None
                previous_ts = ts
                continue

            if current is None or previous_ts is None or ts - previous_ts > gap:
                current = {
                    "start": row["timestamp"],
                    "end": row["timestamp"],
                    "kwh": 0.0,
                    "kwh_outside": 0.0,
                    "peak_kw": kw,
                    "sum_kw": 0.0,
                    "samples": 0,
                    "inside": True,
                }
                runs.append(current)
            current["end"] = row["timestamp"]
            current["kwh"] += kwh
            current["sum_kw"] += kw
            current["peak_kw"] = max(current["peak_kw"], kw)
            current["samples"] += 1
            if not inside:
                # Classified per sample, not per run: a run that starts at
                # 07:30 and ends at 08:30 is half a breach, and rounding it
                # to either whole answer would be a worse number than the
                # one the samples already carry.
                current["kwh_outside"] += kwh
                current["inside"] = False
            previous_ts = ts

        for run in runs:
            start = datetime.fromisoformat(run["start"])
            end = datetime.fromisoformat(run["end"])
            run["minutes"] = round((end - start).total_seconds() / 60.0, 1)
            run["mean_kw"] = round(run.pop("sum_kw") / run["samples"], 3)
            run["kwh"] = round(run["kwh"], 3)
            run["kwh_outside"] = round(run["kwh_outside"], 3)
            run["peak_kw"] = round(run["peak_kw"], 3)

        runs.reverse()
        return {
            "window": {"from_h": from_h, "to_h": to_h},
            "threshold_kw": self._heatpump_run_kw,
            # The span the clamp actually covers. An empty stretch inside it
            # is the pump standing still; an empty stretch outside it is
            # this service not having been there, and the two must not read
            # the same on a chart whose whole point is empty stretches.
            "measured_from": rows[0]["timestamp"] if rows else None,
            "measured_to": rows[-1]["timestamp"] if rows else None,
            "runs": runs,
            "totals": {
                "runs": len(runs),
                "kwh": round(sum(r["kwh"] for r in runs), 3),
                "kwh_outside": round(sum(r["kwh_outside"] for r in runs), 3),
                "minutes": round(sum(r["minutes"] for r in runs), 1),
                # The standing load of the controller, which is the one
                # number here that nobody chose: it runs whether the house
                # wants heat or not.
                "standby_kwh": round(standby_kwh, 3),
                "measured_hours": round(measured_h, 2),
            },
        }

    def wallbox_sessions(self, since_iso: str, gap_minutes: int = 15) -> list[dict]:
        """Contiguous charging runs.

        Taken from the clamp meter on the charger's feed where there is one,
        which is from 31 Oct 2026 - so start, end, duration, energy and peak
        are all measured, at the resolution of the sampling interval.

        Never from the charger's own status flag, in either era. The flag
        reads "Charging" for a plugged-in car that has finished and stays
        there with 0A on every phase, so a one-hour charge on 27 Oct 2026
        showed up as a 63-minute session starting two minutes after it ended,
        carrying 0 kWh.

        Before the clamp, the runs are reconstructed from the charger's
        counter placed in time by the house load (see _wallbox_series), and
        peak_kw there is derived rather than measured - the charger cannot be
        read while it is delivering anything. Each session carries "measured"
        so the two are never presented as the same kind of fact.

        A session ends when the samples stop for longer than gap_minutes, so
        a service restart mid-charge does not read as one long session with a
        hole in it.
        """
        return [
            session
            for session in self._sessions(since_iso, gap_minutes)
            # Below the counter's own resolution it cannot be a charge the
            # counter saw - it is a share the weighting gave an appliance.
            if session["energy_kwh"] >= MIN_SESSION_KWH
        ]

    def _sessions(self, since_iso: str, gap_minutes: int) -> list[dict]:
        sessions: list[dict] = []
        current: dict | None = None
        previous_ts: datetime | None = None
        gap = timedelta(minutes=gap_minutes)

        for sample in self._wallbox_series(since_iso):
            if sample["metered"]:
                # The clamp sees the charger's own standby too - measured at
                # 4-7W with nothing plugged in - so a measured sample needs
                # the same threshold the live gate applies, not merely "some
                # energy". Without it every day would be one session 24
                # hours long.
                if sample["kw"] < self._wallbox_meter_busy_kw:
                    continue
            elif not sample["kwh"]:
                continue
            ts = datetime.fromisoformat(sample["timestamp"])
            if current is None or previous_ts is None or ts - previous_ts > gap:
                current = {
                    "start": sample["timestamp"],
                    "end": sample["timestamp"],
                    "energy_kwh": 0.0,
                    "peak_kw": 0.0,
                    "samples": 0,
                    "measured": True,
                }
                sessions.append(current)
            current["end"] = sample["timestamp"]
            current["energy_kwh"] += sample["kwh"]
            current["peak_kw"] = max(current["peak_kw"], sample["kw"])
            current["samples"] += 1
            # One reconstructed sample makes the whole session reconstructed.
            # A session that straddles the day the clamp went in is the only
            # way this happens, and the weaker claim is the true one.
            if not sample["metered"]:
                current["measured"] = False
            previous_ts = ts

        for session in sessions:
            start = datetime.fromisoformat(session["start"])
            end = datetime.fromisoformat(session["end"])
            session["duration_min"] = round((end - start).total_seconds() / 60.0, 1)
            session["energy_kwh"] = round(session["energy_kwh"], 3)
            session["peak_kw"] = round(session["peak_kw"], 2)

        sessions.reverse()
        return sessions

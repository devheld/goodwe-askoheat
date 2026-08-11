from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from .askoheat_client import AskoHeatController
from .config import Settings
from .database import HistoryStore, Reading
from .goodwe_client import GoodWeCloudReader

logger = logging.getLogger(__name__)


class SurplusController:
    """Core control loop: increases/decreases the AskoHeat's heater step
    step by step, depending on whether there is PV surplus (no grid
    import/export and the battery is charging rather than discharging).

    Runs on its own background thread (not as an asyncio task): the GoodWe
    and Modbus libraries are synchronous, and set_step() internally sleeps
    ASKO_DELAY seconds waiting for the readback - that would block FastAPI's
    asyncio event loop.
    """

    # A commanded step should draw noticeably more than this once actually
    # heating; anything below is treated as "not really heating" rather than
    # trusting small register noise.
    MIN_ACTIVE_LOAD_W = 50

    def __init__(
        self,
        settings: Settings,
        reader: GoodWeCloudReader,
        askoheat: AskoHeatController,
        store: HistoryStore,
    ) -> None:
        self._settings = settings
        self._reader = reader
        self._askoheat = askoheat
        self._store = store
        self._current_step = settings.step_min
        self._lock = threading.Lock()
        self._latest: dict | None = None
        self._stop_event = threading.Event()

    @property
    def latest(self) -> dict | None:
        with self._lock:
            return self._latest

    def stop(self) -> None:
        self._stop_event.set()

    def run_forever(self) -> None:
        self._connect_with_retry()
        try:
            while not self._stop_event.is_set():
                self._tick()
                self._stop_event.wait(self._settings.poll_interval_s)
        finally:
            self._askoheat.close()

    def _connect_with_retry(self) -> None:
        """connects to the AskoHeat, retrying every poll_interval_s on
        failure instead of letting the background thread die"""
        while not self._stop_event.is_set():
            try:
                self._askoheat.connect()
                return
            except Exception:
                logger.exception(
                    "Failed to connect to AskoHeat, retrying in %ss",
                    self._settings.poll_interval_s,
                )
                self._stop_event.wait(self._settings.poll_interval_s)

    def _describe_status(
        self,
        has_surplus: bool,
        readings,
        step_before: int,
        heater_load_w: int,
        askoheat_temp_c: float | None,
    ) -> tuple[str, str]:
        """derives a short status code + human-readable text for the dashboard,
        explaining what the control loop is doing this tick and why"""
        if heater_load_w < 0:
            return "error", "Failed to control the AskoHeat - check the connection"

        # A commanded step that isn't actually drawing power almost always
        # means the AskoHeat's own thermostat has cut the heating elements
        # off (see the temperature limit reference line on the temperature
        # chart). Surface that regardless of what the surplus calculation
        # says, since "we're commanding heat but nothing is happening" is the
        # most actionable thing the dashboard can show.
        if self._current_step > 0 and heater_load_w < self.MIN_ACTIVE_LOAD_W:
            temp_note = f" ({askoheat_temp_c:.0f}°C)" if askoheat_temp_c is not None else ""
            return (
                "capped",
                f"Step {self._current_step}/{self._settings.step_max} commanded but "
                f"AskoHeat is not heating{temp_note} - likely at its temperature limit",
            )

        if has_surplus:
            if self._current_step > step_before:
                return "increasing", "PV surplus available - increasing heater step"
            return "max", "PV surplus available - heater already at maximum step"

        if not readings.battery_charging:
            reason = "battery discharging"
        elif abs(readings.grid_kw) >= self._settings.zero_threshold_kw:
            reason = "grid flow detected"
        else:
            reason = "no surplus"

        if self._current_step < step_before:
            return "decreasing", f"No surplus ({reason}) - reducing heater step"
        return "off", f"No surplus ({reason}) - heater off"

    def _tick(self) -> None:
        try:
            readings = self._reader.read()
        except Exception:
            logger.exception("Failed to fetch GoodWe data")
            return

        # Surplus only when there is no grid import/export AND the battery
        # is charging rather than discharging (battery_charging comes from
        # the energy balance derived in GoodWeCloudReader).
        has_surplus = (
            abs(readings.grid_kw) < self._settings.zero_threshold_kw
            and readings.battery_charging
        )

        step_before = self._current_step
        if has_surplus:
            self._current_step = min(self._current_step + 1, self._settings.step_max)
        else:
            self._current_step = max(self._current_step - 1, self._settings.step_min)

        try:
            heater_load_w = self._askoheat.set_step(self._current_step)
        except Exception:
            logger.exception("Failed to control the AskoHeat")
            heater_load_w = -1

        try:
            askoheat_temp_c = self._askoheat.read_temperature_c()
        except Exception:
            logger.exception("Failed to read the AskoHeat temperature")
            askoheat_temp_c = None

        status, status_text = self._describe_status(
            has_surplus, readings, step_before, heater_load_w, askoheat_temp_c
        )

        timestamp = datetime.now(timezone.utc).isoformat()
        reading = Reading(
            timestamp=timestamp,
            pv_kw=readings.pv_kw,
            consumption_kw=readings.consumption_kw,
            battery_charge_kw=readings.battery_charge_kw,
            battery_discharge_kw=readings.battery_discharge_kw,
            grid_kw=readings.grid_kw,
            battery_charging=readings.battery_charging,
            step=self._current_step,
            heater_load_w=heater_load_w,
            askoheat_temp_c=askoheat_temp_c,
        )
        self._store.insert(reading)

        with self._lock:
            self._latest = {
                "timestamp": timestamp,
                "pv_kw": readings.pv_kw,
                "consumption_kw": readings.consumption_kw,
                "battery_charge_kw": readings.battery_charge_kw,
                "battery_discharge_kw": readings.battery_discharge_kw,
                "grid_kw": readings.grid_kw,
                "battery_charging": readings.battery_charging,
                "step": self._current_step,
                "heater_load_w": heater_load_w,
                "askoheat_temp_c": askoheat_temp_c,
                "status": status,
                "status_text": status_text,
            }

        logger.info(
            "step=%s pv=%.2fkW consumption=%.2fkW battery_charge=%.2fkW battery_discharge=%.2fkW grid=%.2fkW -> askoheat_load=%sW temp=%sC [%s] %s",
            self._current_step,
            readings.pv_kw,
            readings.consumption_kw,
            readings.battery_charge_kw,
            readings.battery_discharge_kw,
            readings.grid_kw,
            heater_load_w,
            f"{askoheat_temp_c:.1f}" if askoheat_temp_c is not None else "?",
            status,
            status_text,
        )

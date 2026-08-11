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
            }

        logger.info(
            "step=%s pv=%.2fkW consumption=%.2fkW battery_charge=%.2fkW battery_discharge=%.2fkW grid=%.2fkW -> askoheat_load=%sW temp=%sC",
            self._current_step,
            readings.pv_kw,
            readings.consumption_kw,
            readings.battery_charge_kw,
            readings.battery_discharge_kw,
            readings.grid_kw,
            heater_load_w,
            f"{askoheat_temp_c:.1f}" if askoheat_temp_c is not None else "?",
        )

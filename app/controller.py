from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from .askoheat_client import AskoHeatController
from .config import Settings
from .database import HistoryStore, Reading
from .goodwe_client import GoodWeModbusReader
from .shelly_client import ShellyClient
from .wallbox_client import STATUS_TEXT, GoodWeWallboxReader

logger = logging.getLogger(__name__)


class SurplusController:
    """Core control loop: increases/decreases the AskoHeat's heater step
    step by step, depending on whether there is PV surplus left over once
    the house battery has been charged.

    The battery has priority: it is charged first, and only once it has
    reached battery_full_soc_pct does any surplus go into the water tank.
    From that point the battery no longer absorbs the surplus, which is
    exported instead - so the heater is regulated against grid feed-in.
    See _decide() for the full rule set.

    Runs on its own background thread (not as an asyncio task): the GoodWe
    and Modbus libraries are synchronous, and set_step() internally sleeps
    ASKO_DELAY seconds waiting for the readback - that would block FastAPI's
    asyncio event loop.
    """

    # A commanded step should draw noticeably more than this once actually
    # heating; anything below is treated as "not really heating" rather than
    # trusting small register noise.
    MIN_ACTIVE_LOAD_W = 50

    # The AskoHeat's own thermostat cuts the heating elements off somewhere
    # around this temperature (matches ASKOHEAT_MAX_TEMP_C in app.js, shown
    # there as the temperature chart's reference line). Only blame "at its
    # temperature limit" when the reading is actually close to this - a
    # step commanded with zero load well below it is something else (seen in
    # practice: a step-0 restart that doesn't draw power again for tens of
    # minutes for reasons we can't diagnose from the Modbus data alone).
    DEVICE_TEMP_LIMIT_C = 65
    NEAR_DEVICE_LIMIT_MARGIN_C = 5

    def __init__(
        self,
        settings: Settings,
        reader: GoodWeModbusReader,
        askoheat: AskoHeatController,
        store: HistoryStore,
        wallbox: GoodWeWallboxReader | None = None,
        shelly_meters: dict[str, ShellyClient] | None = None,
    ) -> None:
        self._settings = settings
        self._reader = reader
        self._askoheat = askoheat
        self._store = store
        # Read-only passenger on the control loop: polled for the dashboard,
        # never consulted for a heating decision. See GoodWeWallboxReader on
        # why its connection is held open rather than reopened per tick.
        self._wallbox = wallbox
        # Last recorded charger state, so the event log keeps transitions
        # instead of one row per poll. None until the first reading.
        self._wallbox_state: dict | None = None
        self._wallbox_snapshot: dict | None = None
        self._wallbox_polled_at: float | None = None
        self._wallbox_poll_suspended = False
        # Starts satisfied: a fresh process has not just watched a charge
        # end, so the first quiet tick may read the charger straight away
        # instead of leaving the panel blank for the first six minutes.
        self._wallbox_quiet_ticks = settings.wallbox_idle_ticks
        # The clamp meters, keyed by what each one is on: "grid",
        # "heatpump", "wallbox". Read-only and never consulted for a heating
        # decision, but unlike the charger they are read on every tick:
        # plain HTTP, no session limit, and nothing they do can interrupt
        # anything.
        self._shelly_meters: dict[str, ShellyClient] = dict(shelly_meters or {})
        # Last snapshot per meter, for _device_health - which reports what
        # the last poll found rather than asking again.
        self._shelly_snapshots: dict[str, dict] = {}
        self._current_step = settings.step_min
        self._lock = threading.Lock()
        self._latest: dict | None = None
        self._stop_event = threading.Event()
        # True for exactly the first tick after (re)reading the step from the
        # device: that tick should observe/confirm against current PV
        # conditions instead of blindly nudging the step up or down.
        self._pending_initial_hold = False

    @property
    def latest(self) -> dict | None:
        with self._lock:
            return self._latest

    def stop(self) -> None:
        self._stop_event.set()

    def run_forever(self) -> None:
        # No device is waited for. Each one reconnects on its own (see
        # app/modbus_link.py for the two permanent connections; the wallbox
        # opens and closes a connection per poll), and _tick() treats a
        # device that does not answer as a reportable condition rather than
        # a fatal one. Blocking here instead meant an immersion heater that
        # was simply switched off took the whole dashboard down with it: the
        # loop never reached its first tick, /api/live had nothing to return
        # and answered 503 for an hour while the inverter was reachable the
        # entire time.
        self._connect_once("GoodWe inverter", self._reader.connect)
        self._connect_once("AskoHeat", self._askoheat.connect)
        self._current_step = self._read_initial_step()
        self._pending_initial_hold = True
        try:
            while not self._stop_event.is_set():
                self._tick()
                self._stop_event.wait(self._settings.poll_interval_s)
        finally:
            self._askoheat.close()
            self._reader.close()
            if self._wallbox is not None:
                self._wallbox.close()

    def _connect_once(self, device_name: str, connect_fn) -> None:
        """Connect if the device answers, carry on without it if it does not.

        Every device here degrades the service rather than stopping it, so
        none of them is worth blocking on. The connection is re-established
        by the first operation that succeeds after the device comes back.
        """
        try:
            connect_fn()
        except Exception:
            logger.warning(
                "%s did not answer at startup - continuing without it, "
                "operations will reconnect once it is back",
                device_name,
                exc_info=True,
            )

    def _device_health(self, inverter_error: str | None = None) -> dict:
        """Which devices answered last time they were asked.

        Reported rather than probed: asking three devices whether they are
        there would cost three connect timeouts per request.
        """
        wallbox = self._wallbox_snapshot or {}
        health = {
            "inverter": {
                "online": inverter_error is None and self._reader.online,
                "error": inverter_error,
            },
            "askoheat": {
                "online": self._askoheat.online,
                "error": None if self._askoheat.online else "Not reachable",
            },
        }
        if self._wallbox is not None:
            health["wallbox"] = {
                "online": bool(wallbox.get("online")),
                "error": wallbox.get("error"),
            }
        for role in self._shelly_meters:
            snapshot = self._shelly_snapshots.get(role) or {}
            health["shelly_" + role] = {
                "online": bool(snapshot.get("online")),
                "error": snapshot.get("error"),
            }
        return health

    def _publish_inverter_failure(self, error: str) -> None:
        """Publish a snapshot without measurements, so the dashboard can say
        what is missing.

        Without this /api/live had nothing to return and answered 503, which
        tells a reader that the service is broken rather than that one device
        is unplugged. The measurements are left empty rather than carried
        over from the last good tick: a PV figure from twenty minutes ago is
        worse than none, because nothing on the page would say how old it is.
        """
        # The charger is on its own clock and its own connection, so it is
        # still worth reading - a tick that gave up before reaching it was
        # how an unreachable inverter also blanked the wallbox panel, for no
        # reason other than the order of the lines in _tick().
        wallbox = self._poll_wallbox()
        # The clamp meters matter most exactly here. They measure on their
        # own, so with the inverter gone the page can still say what the
        # house is drawing and where most of it is going.
        shelly = self._poll_shelly_meters()
        empty = dict.fromkeys(
            (
                "pv_kw",
                "consumption_kw",
                "battery_charge_kw",
                "battery_discharge_kw",
                "grid_kw",
                "grid_import_kw",
                "grid_export_kw",
                "battery_charging",
                "battery_state",
                "battery_soc_pct",
                "heater_load_w",
                "askoheat_temp_c",
            )
        )
        with self._lock:
            self._latest = {
                **empty,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "step": self._current_step,
                "status": "error",
                "status_text": (
                    f"Inverter unreachable ({error}) - no measurements until it "
                    f"answers again, retrying every {self._settings.poll_interval_s}s"
                ),
                "wallbox": wallbox,
                "shelly": shelly,
                "devices": self._device_health(inverter_error=error),
            }

    def _read_initial_step(self) -> int:
        """reads the AskoHeat's currently commanded step so a restart of this
        app picks up where the device already is, instead of jumping back to
        step_min and slowly ramping back up over several poll cycles"""
        try:
            step = self._askoheat.read_step()
        except Exception:
            logger.exception(
                "Failed to read the AskoHeat's current step, starting from step_min"
            )
            return self._settings.step_min

        clamped_step = max(self._settings.step_min, min(step, self._settings.step_max))
        if clamped_step != step:
            logger.warning(
                "AskoHeat reported step %s, outside [%s, %s] - clamping",
                step, self._settings.step_min, self._settings.step_max,
            )
        logger.info("Starting from the AskoHeat's current step: %s", clamped_step)
        return clamped_step

    def _battery_full_threshold(self, heating: bool) -> float:
        """SOC at or above which the battery counts as full enough to allow
        heating.

        Lower while a heating cycle is already running: the heater's own load
        is exactly what stops the battery from charging further and pulls the
        SOC back down, so a single threshold would switch heating on and off
        every few minutes.
        """
        if heating:
            return self._settings.battery_keep_heating_soc_pct
        return self._settings.battery_full_soc_pct

    def _decide(self, readings, step_before: int) -> tuple[str, str]:
        """decides this tick's step change - "up", "down" or "hold" - along
        with a short reason code the status text is built from.

        Battery first: no surplus goes into the water tank before the house
        battery has reached battery_full_soc_pct (battery_keep_heating_soc_pct
        while a cycle is already running, see _battery_full_threshold). That
        matters from autumn onwards, when a day's surplus is no longer enough
        to both fill the battery and heat the tank - in summer the battery is
        full by midday anyway and this barely changes when heating starts.

        The ORDER of the checks decides which reason is reported, not what
        happens: every branch below that returns "down" would return "down"
        in any order. No-surplus is checked before the SOC because it is the
        binding reason when both are true - with the house importing there is
        nothing charging the battery either, so "battery has priority" would
        name a condition that is not what is holding heating back. Measured
        on this installation's own history before the change: of all ticks
        reported as "battery", 78% had the battery not charging at all and
        64% had it actively discharging.

        Once the battery is full it stops absorbing the surplus, and what
        happens to that surplus then decides how the heater is regulated
        (grid_kw is positive while exporting, negative while importing):
          - importing, or the battery discharging to cover the heater: the
            heater is taking more than is available, drop a step;
          - exporting at least surplus_step_margin_kw: clear surplus, add a
            step;
          - exporting less than that: hold, since the smallest step (~600W)
            would not fit into what is left and would pull from the grid;
          - no grid flow at all with the battery full and idle: this is the
            important case on this installation, which runs at 0% feed-in
            (see README). The inverter curtails the PV strings instead of
            exporting, so the surplus is invisible in the readings - PV just
            tracks the load. Add a step and let the inverter release some of
            the curtailed production to cover it; if it turns out there was
            nothing left, the battery starts discharging and the rule above
            takes the step straight back.
        """
        heating = step_before > self._settings.step_min
        soc = readings.battery_soc_pct

        if soc is None:
            # An unknown SOC counts as "not full", which keeps the battery's
            # priority intact instead of heating on a guess.
            return "down", "soc_unknown"

        # No surplus at all, whatever the SOC says.
        if not readings.battery_charging:
            return "down", "battery_discharging"
        if readings.grid_kw <= -self._settings.zero_threshold_kw:
            return "down", "grid_import"

        # Past here there is something to distribute, so the battery's
        # priority really is what keeps it away from the heater.
        if soc < self._battery_full_threshold(heating):
            return "down", "charging_battery"

        if readings.grid_kw >= self._settings.surplus_step_margin_kw:
            return "up", "surplus"
        if readings.grid_kw >= self._settings.zero_threshold_kw:
            return "hold", "balanced"
        return "up", "surplus_curtailed"

    def _describe_status(
        self,
        reason: str,
        readings,
        step_before: int,
        heater_load_w: int,
        askoheat_temp_c: float | None,
        is_initial_hold: bool = False,
        blocked_by_temp: bool = False,
    ) -> tuple[str, str]:
        """derives a short status code + human-readable text for the dashboard,
        explaining what the control loop is doing this tick and why"""
        if heater_load_w < 0:
            return "error", "Failed to control the AskoHeat - check the connection"

        # A commanded step that isn't actually drawing power. Surface that
        # regardless of what the surplus calculation says, since "we're
        # commanding heat but nothing is happening" is the most actionable
        # thing the dashboard can show - but only blame the device's
        # temperature limit when the reading is actually close to it, not
        # whenever this happens (see DEVICE_TEMP_LIMIT_C above).
        if self._current_step > 0 and heater_load_w < self.MIN_ACTIVE_LOAD_W:
            temp_note = f" ({askoheat_temp_c:.0f}°C)" if askoheat_temp_c is not None else ""
            near_device_limit = (
                askoheat_temp_c is not None
                and askoheat_temp_c >= self.DEVICE_TEMP_LIMIT_C - self.NEAR_DEVICE_LIMIT_MARGIN_C
            )
            if near_device_limit:
                return (
                    "capped",
                    f"Step {self._current_step}/{self._settings.step_max} commanded but "
                    f"AskoHeat is not heating{temp_note} - likely at its temperature limit "
                    f"(device cuts off around {self.DEVICE_TEMP_LIMIT_C:.0f}°C)",
                )
            return (
                "capped",
                f"Step {self._current_step}/{self._settings.step_max} commanded but "
                f"AskoHeat is not heating{temp_note} - cause unclear, well below the "
                f"device's ~{self.DEVICE_TEMP_LIMIT_C:.0f}°C temperature limit",
            )

        if blocked_by_temp:
            return (
                "warm",
                f"PV surplus available but AskoHeat still warm "
                f"({askoheat_temp_c:.0f}°C > {self._settings.heating_start_max_temp_c:.0f}°C) "
                f"- waiting to cool down before starting to heat again",
            )

        soc = readings.battery_soc_pct
        soc_text = f"{soc:.0f}%" if soc is not None else "unknown"
        # Taken from battery_state, not battery_charging: the latter is true
        # while the battery sits idle inside the dead band, which is how this
        # text used to report "charging at 9%" with the battery at -4W and
        # the house importing 1.7kW.
        flow_text = {
            "charging": f"charging at {readings.battery_charge_kw:.2f}kW",
            "discharging": f"discharging at {readings.battery_discharge_kw:.2f}kW",
        }.get(readings.battery_state, "idle")

        # Two different thresholds depending on whether a heating cycle was
        # already running, so say which one is actually being applied.
        was_heating = step_before > self._settings.step_min
        threshold = self._battery_full_threshold(was_heating)
        goal = (
            f"needs {threshold:.0f}% to keep heating"
            if was_heating
            else f"heating starts at {threshold:.0f}%"
        )
        battery_full = soc is not None and soc >= threshold

        if is_initial_hold:
            condition = {
                "surplus": "surplus available",
                "surplus_curtailed": "battery full and grid balanced",
                "balanced": "surplus and heater balanced",
                "charging_battery": f"battery at {soc_text}, not full yet",
                "soc_unknown": "battery SOC unavailable",
                "battery_discharging": "battery discharging",
                "grid_import": "importing from the grid",
            }[reason]
            return (
                "holding",
                f"Resumed at step {self._current_step}/{self._settings.step_max} "
                f"(read from device, {condition}) - adjusting from the next cycle",
            )

        heater_note = (
            f"reducing heater step to {self._current_step}"
            if self._current_step < step_before
            else "heater off"
        )

        if reason == "soc_unknown":
            return (
                "battery",
                f"Battery SOC could not be read - treating the battery as not full, "
                f"so it keeps priority ({heater_note})",
            )

        if reason == "charging_battery":
            return (
                "battery",
                f"Battery has priority: at {soc_text} and {flow_text}, "
                f"{goal} ({heater_note})",
            )

        if reason in ("surplus", "surplus_curtailed"):
            source = (
                f"exporting {readings.grid_kw:.2f}kW"
                if reason == "surplus"
                else "battery full and idle, so PV is being curtailed"
            )
            if self._current_step > step_before:
                return (
                    "increasing",
                    f"Battery charged ({soc_text}), {source} - increasing heater step",
                )
            return (
                "max",
                f"Battery charged ({soc_text}), {source} - heater already at "
                f"maximum step",
            )

        if reason == "balanced":
            return (
                "balanced",
                f"Battery charged ({soc_text}), surplus and heater balanced "
                f"({readings.grid_kw:+.2f}kW grid) - holding step "
                f"{self._current_step}/{self._settings.step_max}",
            )

        # The battery's own flow is already in the cause when it is the
        # cause, so don't repeat it there.
        if reason == "battery_discharging":
            cause = f"battery {flow_text}"
            battery_note = f" Battery at {soc_text}, {goal}."
        else:
            cause = f"importing {abs(readings.grid_kw):.2f}kW from the grid"
            battery_note = f" Battery at {soc_text} ({flow_text}), {goal}."
        # Name the second condition too, but only while it is one: below the
        # threshold the heater would stay off even if the surplus came back
        # this minute. Once the battery is full it is just noise.
        if battery_full:
            battery_note = ""
        if self._current_step < step_before:
            return (
                "decreasing",
                f"No surplus ({cause}) - reducing heater step.{battery_note}",
            )
        return "off", f"No surplus ({cause}) - heater off.{battery_note}"

    def _tick(self) -> None:
        try:
            readings = self._reader.read()
        except Exception as exc:
            logger.exception("Failed to fetch GoodWe data")
            # Nothing can be decided or recorded without the inverter, but
            # the dashboard can still be told why.
            self._publish_inverter_failure(str(exc))
            return

        # Read temperature up front (not just after set_step as before) since
        # the heating-start hysteresis below needs it to make the step
        # decision - this also saves the second Modbus round-trip we used to
        # do after set_step().
        try:
            askoheat_temp_c = self._askoheat.read_temperature_c()
        except Exception:
            logger.exception("Failed to read the AskoHeat temperature")
            askoheat_temp_c = None

        step_before = self._current_step
        is_initial_hold = self._pending_initial_hold
        blocked_by_temp = False
        # Battery before water tank, then feed-in regulation - see _decide().
        action, reason = self._decide(readings, step_before)

        if is_initial_hold:
            # First tick after (re)reading the step from the device: hold it
            # and just observe this cycle's conditions instead of blindly
            # nudging it up or down - see _describe_status's "holding" case.
            self._pending_initial_hold = False
        elif action == "up":
            # Hysteresis: only START a new heating cycle (step 0 -> >0) while
            # the tank is at or below heating_start_max_temp_c. Once heating
            # is already running (step > 0), keep adjusting it normally and
            # let the device's own (higher) temperature limit stop it - see
            # the "capped" status above. Without this, heating would start
            # and stop repeatedly right at the device's cutoff temperature.
            starting_new_cycle = step_before == self._settings.step_min
            if (
                starting_new_cycle
                and askoheat_temp_c is not None
                and askoheat_temp_c > self._settings.heating_start_max_temp_c
            ):
                blocked_by_temp = True
            else:
                self._current_step = min(self._current_step + 1, self._settings.step_max)
        elif action == "down":
            self._current_step = max(self._current_step - 1, self._settings.step_min)
        # action == "hold": leave the step where it is (see _decide's dead band)

        try:
            heater_load_w = self._askoheat.set_step(self._current_step)
        except Exception:
            logger.exception("Failed to control the AskoHeat")
            heater_load_w = -1

        status, status_text = self._describe_status(
            reason,
            readings,
            step_before,
            heater_load_w,
            askoheat_temp_c,
            is_initial_hold,
            blocked_by_temp,
        )

        # Dashboard-only, and deliberately after the heating decision above so
        # a slow or unreachable wallbox can never delay or change it. The
        # client swallows its own errors and reports them as a payload.
        # Everything drawn above the heater is taken to be the car, which is
        # what decides whether the charger may be read at all right now - see
        # _poll_wallbox. heater_load_w is -1 when the AskoHeat failed.
        wallbox = self._poll_wallbox(
            readings.consumption_kw - max(heater_load_w, 0) / 1000
        )

        # Same deal as the wallbox: dashboard and history only, after the
        # heating decision, errors swallowed into the payload.
        shelly = self._poll_shelly_meters() or {}
        # Each meter fills its own columns, so the three series stay apart
        # and a query can never pick up the wrong one. A meter that is not
        # installed, or that did not answer, leaves its columns NULL instead
        # of writing a zero that would read as "measured nothing".
        at_grid = shelly.get("grid") or {}
        at_heatpump = shelly.get("heatpump") or {}
        at_wallbox = shelly.get("wallbox") or {}

        timestamp = datetime.now(timezone.utc).isoformat()
        reading = Reading(
            timestamp=timestamp,
            pv_kw=readings.pv_kw,
            consumption_kw=readings.consumption_kw,
            battery_charge_kw=readings.battery_charge_kw,
            battery_discharge_kw=readings.battery_discharge_kw,
            grid_kw=readings.grid_kw,
            battery_charging=readings.battery_charging,
            battery_soc_pct=readings.battery_soc_pct,
            step=self._current_step,
            heater_load_w=heater_load_w,
            askoheat_temp_c=askoheat_temp_c,
            status=status,
            status_text=status_text,
            wallbox_power_kw=(wallbox or {}).get("power_kw"),
            wallbox_status_code=(wallbox or {}).get("status_code"),
            wallbox_charging=(wallbox or {}).get("charging"),
            wallbox_energy_kwh=(wallbox or {}).get("total_energy_kwh"),
            shelly_grid_kw=at_grid.get("total_kw"),
            shelly_phase_kw=at_grid.get("phase_kw"),
            shelly_currents_a=at_grid.get("currents_a"),
            shelly_import_kwh=at_grid.get("import_kwh"),
            shelly_export_kwh=at_grid.get("export_kwh"),
            heatpump_kw=at_heatpump.get("total_kw"),
            heatpump_phase_kw=at_heatpump.get("phase_kw"),
            heatpump_energy_kwh=at_heatpump.get("import_kwh"),
            wallbox_meter_kw=at_wallbox.get("total_kw"),
            wallbox_meter_phase_kw=at_wallbox.get("phase_kw"),
            wallbox_meter_energy_kwh=at_wallbox.get("import_kwh"),
            load_phase_kw=readings.load_phase_kw,
        )
        self._store.insert(reading)
        # A suspended poll carries either the previous snapshot or no reading
        # at all, so there is no transition to record and no complete payload
        # to record it from.
        if wallbox is not None and not wallbox.get("poll_suspended"):
            self._record_wallbox_event(timestamp, wallbox)

        # What the inverter's own four numbers fail to account for: energy in
        # minus energy out. Measured here at a steady 0.12kW with the battery
        # idle and no PV - where no conversion happens at all - rising to
        # 0.18kW while the battery discharges, and flat across the whole
        # discharge range from 0.5 to 13kW. A conversion loss would scale
        # with the power; a fixed draw does not. See README section 8.
        balance_gap_kw = round(
            readings.pv_kw
            + readings.battery_discharge_kw
            - readings.battery_charge_kw
            - readings.consumption_kw
            - readings.grid_kw,
            3,
        )

        with self._lock:
            self._latest = {
                "timestamp": timestamp,
                "pv_kw": readings.pv_kw,
                "consumption_kw": readings.consumption_kw,
                "battery_charge_kw": readings.battery_charge_kw,
                "battery_discharge_kw": readings.battery_discharge_kw,
                "grid_kw": readings.grid_kw,
                # Same quantity, split by direction. The meter reports one
                # signed value (positive = export on this inverter) and the
                # database keeps it that way, but every reader downstream
                # gets named, always-positive numbers - which direction a
                # sign means is a per-vendor convention, a label is not.
                "grid_import_kw": max(0.0, -readings.grid_kw),
                "grid_export_kw": max(0.0, readings.grid_kw),
                "battery_charging": readings.battery_charging,
                "battery_state": readings.battery_state,
                "battery_soc_pct": readings.battery_soc_pct,
                # The house load split over the three phases, from the
                # inverter rather than from a clamp - so the imbalance is
                # visible wherever the clamp meters happen to be.
                "load_phase_kw": (
                    list(readings.load_phase_kw) if readings.load_phase_kw else None
                ),
                "step": self._current_step,
                "heater_load_w": heater_load_w,
                "askoheat_temp_c": askoheat_temp_c,
                "status": status,
                "status_text": status_text,
                "balance_gap_kw": balance_gap_kw,
                "wallbox": wallbox,
                "shelly": shelly,
                "devices": self._device_health(),
            }

        logger.info(
            "step=%s pv=%.2fkW consumption=%.2fkW battery_charge=%.2fkW battery_discharge=%.2fkW soc=%s%% grid=%.2fkW -> askoheat_load=%sW temp=%sC [%s] %s",
            self._current_step,
            readings.pv_kw,
            readings.consumption_kw,
            readings.battery_charge_kw,
            readings.battery_discharge_kw,
            f"{readings.battery_soc_pct:.0f}" if readings.battery_soc_pct is not None else "?",
            readings.grid_kw,
            heater_load_w,
            f"{askoheat_temp_c:.1f}" if askoheat_temp_c is not None else "?",
            status,
            status_text,
        )

    def _poll_shelly_meters(self) -> dict[str, dict] | None:
        """Read every clamp meter, every tick, keyed by what it is on.

        No gate and no slower clock, unlike _poll_wallbox: these are HTTP
        GETs against devices that are happy to be asked and have nothing to
        interrupt. The client never raises, so a meter that is away arrives
        here as a payload with online=False rather than as a missing key -
        which keeps a dead meter apart from one that was never installed.
        """
        if not self._shelly_meters:
            return None
        for role, client in self._shelly_meters.items():
            self._shelly_snapshots[role] = client.as_dict()
        return dict(self._shelly_snapshots)

    def _poll_wallbox(self, vehicle_load_kw: float | None = None) -> dict | None:
        """Read the charger on its own, slower clock - and not at all while a
        car is drawing.

        The heating loop runs every minute, but the charger has only one
        free Modbus slot and the owner's own app competes for it, so it is
        read every wallbox_poll_interval_s instead. Between reads the last
        snapshot is reused, which keeps the dashboard populated and gives
        the energy integration a value to carry forward.

        Reading ANY register interrupts a charge in progress on this
        hardware. The box drops the charging current to 0A while still
        reporting status 3 ("Charging"), and the car takes current again
        around two minutes after the TCP connection closes. Known on the
        GW11K-HCA as evcc discussion #13970, and from the other side on the
        G2 as #30669, where opening a connection at all stops the charge.

        Measured here on 25 Oct 2026: charges ran 3.5 minutes and paused 2.3
        minutes, a 5.9-minute cycle that is exactly this poll interval
        rounded up to the 70s tick grid, with every pause starting one tick
        after a poll. The rhythm was identical whether the energy came from
        the grid at 16kW, from the house battery at zero grid import, or
        from PV, and identical across every import limit tried - none of
        those was the cause. With polling off the same car then charged 17.6
        minutes without a break, against a previous best of 3.6 minutes in
        four days of data.

        So while a car draws, the charger is left alone and the last
        snapshot is reused. The cost is a dashboard panel that freezes
        during a charge; the alternative is a car that charges at 58% duty.

        Both strategies are kept and WALLBOX_POLL_MODE picks one. The above
        is "gated". In "direct" the charger is read every
        wallbox_poll_interval_s whatever the house is doing - the behaviour
        from before the fault was found. That mode exists because the fault
        is the charger's own: a firmware change can end it, and the only way
        to find out is to read again and watch whether a charge survives. It
        is a test, so the default is "gated" and anything unrecognised lands
        there too.
        """
        if self._wallbox is None:
            return None

        if self._settings.wallbox_poll_mode == "direct":
            self._wallbox_poll_suspended = False
            # Leave the gated counter at rest. Switching back is a restart,
            # and the first tick after it must not start out believing a
            # charge is already under way.
            self._wallbox_quiet_ticks = self._settings.wallbox_idle_ticks
            if self._wallbox_poll_due(self._settings.wallbox_poll_interval_s):
                self._read_wallbox()
            if self._wallbox_snapshot is None:
                return None
            return {
                **self._wallbox_snapshot,
                "poll_suspended": False,
                "poll_mode": "direct",
            }

        if vehicle_load_kw is None:
            # No fresh reading to judge by, so the inverter is unreachable.
            # Keep whatever the last tick decided rather than risk cutting a
            # charge that is probably still running.
            busy = self._wallbox_poll_suspended
        else:
            drawing = vehicle_load_kw >= self._settings.wallbox_busy_load_kw
            self._wallbox_quiet_ticks = 0 if drawing else self._wallbox_quiet_ticks + 1
            # One quiet tick is not the end of a charge - it may be a pause,
            # and reading into a pause is what stops the car coming back.
            # Wait out wallbox_idle_ticks before calling it over.
            busy = (
                drawing
                or self._wallbox_quiet_ticks < self._settings.wallbox_idle_ticks
            )
        self._wallbox_poll_suspended = busy

        if busy:
            if self._wallbox_snapshot is None:
                # Restarted mid-charge, so there is nothing to reuse. Polling
                # once just to fill the panel would cut the charge, which is
                # the whole thing this is avoiding - report the gap instead.
                return {
                    "poll_suspended": True,
                    "awaiting_first_read": True,
                    "poll_mode": "gated",
                }
            return {
                **self._wallbox_snapshot,
                "poll_suspended": True,
                "poll_mode": "gated",
            }

        # Any car on the cable is read on a slower clock. Parked, there is
        # nothing changing to catch; charging, the load estimate has already
        # failed to notice (a single-phase car at the 6A minimum draws
        # 1.38kW, under the threshold) and every read costs it about two
        # minutes, so the interval is the only brake left.
        interval = self._settings.wallbox_poll_interval_s
        last = self._wallbox_snapshot or {}
        if last.get("vehicle_connected"):
            interval = max(interval, self._settings.wallbox_idle_poll_interval_s)

        if self._wallbox_poll_due(interval):
            self._read_wallbox()
            # The load threshold cannot see a single-phase car at the 6A
            # minimum (1.38kW), so where the reading and the estimate
            # disagree, believe the reading and stop reading.
            if self._charger_drawing(self._wallbox_snapshot):
                self._wallbox_poll_suspended = True
                self._wallbox_quiet_ticks = 0
                return {
                    **self._wallbox_snapshot,
                    "poll_suspended": True,
                    "poll_mode": "gated",
                }
        if self._wallbox_snapshot is None:
            return None
        return {
            **self._wallbox_snapshot,
            "poll_suspended": False,
            "poll_mode": "gated",
        }

    def _wallbox_poll_due(self, interval: float) -> bool:
        """Has the charger's own clock come round again?

        Claims the slot as a side effect, so a read that then fails waits
        out the whole interval instead of retrying on the next tick.
        """
        now = time.monotonic()
        if (
            self._wallbox_polled_at is not None
            and now - self._wallbox_polled_at < interval
        ):
            return False
        self._wallbox_polled_at = now
        return True

    def _read_wallbox(self) -> None:
        """Take one reading and keep it, whatever happens.

        read() already turns a failure into an offline snapshot, so reaching
        the handler means something unforeseen - a hostname that stopped
        resolving, say. The charger is the one device on this installation
        that controls nothing, so it is never worth losing a tick over.
        """
        try:
            self._wallbox_snapshot = self._wallbox.as_dict()
        except Exception as exc:
            logger.exception("Unexpected failure while reading the wallbox")
            self._wallbox_snapshot = {
                "online": False,
                "error": str(exc),
                "status_code": None,
                "status_text": None,
                "vehicle_connected": None,
                "charging": None,
                "power_kw": None,
                "total_energy_kwh": None,
                "charge_mode": None,
            }

    @staticmethod
    def _charger_drawing(snapshot: dict | None) -> bool:
        """Is current actually flowing?

        Deliberately not the charger's own "charging" flag: this box reports
        status 3 ("Charging") throughout a pause and for a plugged-in car
        that has finished, with 0 A on every phase. Only the power and the
        phase currents distinguish those from a live charge.
        """
        if not snapshot:
            return False
        if (snapshot.get("power_kw") or 0) > 0:
            return True
        return any((a or 0) > 0 for a in snapshot.get("currents_a") or ())

    def _record_wallbox_event(self, timestamp: str, wallbox: dict) -> None:
        """Append to the charger's event log, but only when something changed.

        The per-poll power already lives in the readings table; what that
        table cannot answer afterwards is when a car was plugged in, when a
        charge actually started, or when the box went quiet - so one row is
        written per transition, with the first tick after a restart recorded
        as a startup snapshot rather than as a transition that may never
        have happened.
        """
        current = {
            "online": wallbox["online"],
            "status_code": wallbox["status_code"],
            "charging": wallbox["charging"],
            "charge_mode": wallbox["charge_mode"],
        }
        previous = self._wallbox_state
        if previous == current:
            return
        self._wallbox_state = current

        if previous is None:
            event = "startup"
            detail = "Service started, wallbox %s" % (
                wallbox["status_text"] if current["online"] else "unreachable"
            )
        elif previous["online"] != current["online"]:
            event = "connection"
            detail = (
                "Wallbox reachable again"
                if current["online"]
                else "Wallbox unreachable: %s" % (wallbox.get("error") or "no answer")
            )
        elif previous["charging"] != current["charging"]:
            event = "charging"
            detail = "Charging started" if current["charging"] else "Charging stopped"
        elif previous["status_code"] != current["status_code"]:
            event = "status"
            detail = "%s -> %s" % (
                STATUS_TEXT.get(previous["status_code"], previous["status_code"]),
                wallbox["status_text"],
            )
        else:
            event = "mode"
            detail = "Charge mode %s -> %s" % (
                previous["charge_mode"] or "?",
                current["charge_mode"] or "?",
            )

        self._store.insert_wallbox_event(
            timestamp=timestamp,
            event=event,
            detail=detail,
            status_code=current["status_code"],
            status_text=wallbox["status_text"],
            charge_mode=current["charge_mode"],
            power_kw=wallbox["power_kw"],
            energy_kwh=wallbox["total_energy_kwh"],
        )

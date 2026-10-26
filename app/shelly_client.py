from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShellyReading:
    """One snapshot of the grid connection point.

    Everything here is in the project's sign convention, matching
    Readings.grid_kw: positive is export, negative is import. The device
    itself reports the opposite - see ShellyClient.
    """

    online: bool
    error: str | None = None
    grid_kw: float | None = None
    phase_kw: tuple[float, float, float] | None = None
    currents_a: tuple[float, float, float] | None = None
    voltages_v: tuple[float, float, float] | None = None
    power_factors: tuple[float, float, float] | None = None
    # Cumulative, since the device was installed. Exact where an integrated
    # power curve is not: the counters do not care how often they are read.
    import_kwh: float | None = None
    export_kwh: float | None = None


class ShellyClient:
    """A second opinion on the grid connection point.

    A Shelly 3EM (SHEM-3, firmware v1.14) with its clamps on the three main
    phases, read over plain HTTP from /status. No authentication, nothing
    written, no registers - unlike the charger this device does not mind
    being asked.

    It exists because the inverter was otherwise the only witness to its own
    numbers, and those numbers do not add up: battery discharge minus house
    load minus grid leaves a constant 0.12-0.19kW unaccounted for, in every
    operating state, at every discharge level from 0.5 to 13kW (see README
    section 8). Two independent meters that agree at the connection point
    rule out the obvious suspect, a miscalibrated grid measurement.

    Sign convention. The device reports import as positive power and
    accumulates export in a separate counter. Confirmed on 26 Oct 2026 by
    watching which counter moved: L1 and L2 at -160/-210W fed
    total_returned, L3 at +380W fed total. Readings.grid_kw is the other way
    round (negative while importing, verified against a 17.45kW house load
    reading -9.47kW), so the sign is flipped once here, on the way in, and
    every value this class hands out is export-positive like the rest of the
    project.

    Accuracy. Against the inverter's register 35139, 14 samples over two and
    a half minutes agreed to 5W on average, spread -20 to +27W, while the
    connection point sat near zero. That pins the zero point, not the scale:
    at a real flow of several kW the two have not been compared yet, and the
    logged history will do it on its own.

    What the clamps see is the current through the main fuse, which is what
    makes this the right instrument for the 25A limit: the house can draw
    17kW with nothing crossing the meter, and the inverter's active power
    cannot tell how much current that takes. Measured here at a connection
    point summing to -15W, the individual phases still carried 1.3, 1.3 and
    2.0A at power factors of -0.45 to 0.77 - the phases are not balanced and
    the current is not proportional to the active power.
    """

    def __init__(self, host: str, port: int = 80, timeout: float = 3.0) -> None:
        self._url = f"http://{host}:{port}/status"
        self._timeout = timeout

    def read(self) -> ShellyReading:
        """Never raises. A meter that is away is an ordinary condition."""
        try:
            with urllib.request.urlopen(self._url, timeout=self._timeout) as resp:
                payload = json.load(resp)
        except Exception as exc:
            logger.warning("Shelly read failed: %s", exc)
            return ShellyReading(online=False, error=f"{type(exc).__name__}: {exc}")
        try:
            return self._decode(payload)
        except Exception as exc:
            logger.exception("Shelly returned a payload this cannot read")
            return ShellyReading(online=False, error=str(exc))

    @staticmethod
    def _decode(payload: dict) -> ShellyReading:
        meters = payload.get("emeters") or []
        if len(meters) != 3:
            raise ValueError(f"expected three phases, got {len(meters)}")
        # A clamp that is not reading reports is_valid=false and a power of
        # 0. Dropping the whole snapshot is the honest move: a missing phase
        # would otherwise look like a phase carrying nothing, which is
        # exactly the error this device is here to rule out.
        invalid = [i + 1 for i, m in enumerate(meters) if not m.get("is_valid")]
        if invalid:
            raise ValueError(f"phase(s) {invalid} reported is_valid=false")

        phase_w = tuple(float(m["power"]) for m in meters)
        # total_power is the device's own sum and is what its display shows;
        # falling back to the sum of the phases keeps this working on
        # firmware that does not report it.
        total_w = float(payload.get("total_power", sum(phase_w)))

        return ShellyReading(
            online=True,
            grid_kw=round(-total_w / 1000, 3),
            phase_kw=tuple(round(-w / 1000, 3) for w in phase_w),
            currents_a=tuple(round(float(m["current"]), 2) for m in meters),
            voltages_v=tuple(round(float(m["voltage"]), 1) for m in meters),
            power_factors=tuple(round(float(m["pf"]), 2) for m in meters),
            # The counters are in Wh and count both directions separately,
            # so neither is signed and neither gets flipped.
            import_kwh=round(sum(float(m["total"]) for m in meters) / 1000, 3),
            export_kwh=round(sum(float(m["total_returned"]) for m in meters) / 1000, 3),
        )

    def as_dict(self) -> dict:
        """Snapshot shaped for /api/live and the dashboard."""
        r = self.read()
        return {
            "online": r.online,
            "error": r.error,
            "grid_kw": r.grid_kw,
            "phase_kw": list(r.phase_kw) if r.phase_kw else None,
            "currents_a": list(r.currents_a) if r.currents_a else None,
            "voltages_v": list(r.voltages_v) if r.voltages_v else None,
            "power_factors": list(r.power_factors) if r.power_factors else None,
            "import_kwh": r.import_kwh,
            "export_kwh": r.export_kwh,
            "max_current_a": max(r.currents_a) if r.currents_a else None,
        }

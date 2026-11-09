from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MyStromReading:
    """One snapshot of the plug the balcony inverter is on.

    power_kw is always positive, because the device cannot tell direction.
    See MyStromClient on why that is safe here and nowhere else.
    """

    online: bool
    error: str | None = None
    power_kw: float | None = None
    relay: bool | None = None
    temperature_c: float | None = None
    # The device's own counter, in kWh. Cumulative since it last booted and
    # therefore not a lifetime total - boot_id changes when it reset. Kept
    # as a cross-check against the integrated power, never as the figure
    # the energy page or the money rests on. See the class docstring.
    energy_since_boot_kwh: float | None = None
    boot_id: str | None = None
    uptime_s: float | None = None


class MyStromClient:
    """A myStrom WiFi switch (type 106, firmware 4.x) over plain HTTP.

    Read-only: this never switches the relay. The plug carries a 600 W
    balcony inverter, and the one thing worse than not measuring it would
    be turning it off from a dashboard.

    **The device cannot tell direction.** `power` is an unsigned magnitude,
    so 600 W flowing out of an inverter and 600 W going into a heater look
    identical to it. Nothing in the reading proves which one this is; what
    establishes it is that this client is only constructed when
    MYSTROM_BALCONY_HOST is set, and that setting is the statement "the
    balcony inverter is on this plug". Point it at an ordinary appliance
    and every watt will be booked as production.

    **`Ws` is not a counter and is deliberately ignored.** The field is the
    average energy per second *since the last call to /report*, so its
    value depends on the polling interval and, worse, it is consumed by
    whoever read it last: with Home Assistant polling the same plug, the
    two readers would split the deltas and both of their totals would be
    short. Energy here is integrated from `power` over the interval since
    the previous sample, capped per sample, exactly like the house load and
    the AskoHeat - see HistoryStore.energy_buckets.

    `energy_since_boot` is a real cumulative counter and is recorded, but
    only as the cross-check: it restarts at zero whenever the plug loses
    power, which a balcony inverter's plug does more often than most, and
    `boot_id` is what makes that detectable rather than invisible.
    """

    def __init__(self, host: str, timeout: float = 3.0) -> None:
        self._url = f"http://{host}/report"
        self._timeout = timeout

    def read(self) -> MyStromReading:
        """Never raises. A plug that is away is an ordinary condition."""
        try:
            with urllib.request.urlopen(self._url, timeout=self._timeout) as resp:
                payload = json.load(resp)
        except Exception as exc:
            logger.warning("myStrom read failed: %s", exc)
            return MyStromReading(online=False, error=f"{type(exc).__name__}: {exc}")
        try:
            return self._decode(payload)
        except Exception as exc:
            logger.exception("myStrom returned a payload this cannot read")
            return MyStromReading(online=False, error=str(exc))

    @staticmethod
    def _decode(payload: dict) -> MyStromReading:
        if "power" not in payload:
            raise ValueError("no 'power' in the report")
        watts = float(payload["power"])
        # Negative would mean the firmware started signing this field, which
        # it does not. Clamped rather than trusted, so a surprise can never
        # subtract from a day's production.
        energy_ws = payload.get("energy_since_boot")
        return MyStromReading(
            online=True,
            power_kw=round(max(0.0, watts) / 1000.0, 4),
            relay=bool(payload.get("relay")) if "relay" in payload else None,
            temperature_c=(
                round(float(payload["temperature"]), 1)
                if payload.get("temperature") is not None
                else None
            ),
            energy_since_boot_kwh=(
                round(float(energy_ws) / 3_600_000.0, 4) if energy_ws is not None else None
            ),
            boot_id=payload.get("boot_id"),
            uptime_s=(
                float(payload["time_since_boot"])
                if payload.get("time_since_boot") is not None
                else None
            ),
        )

    def as_dict(self) -> dict:
        """Snapshot shaped for /api/live and the dashboard."""
        r = self.read()
        return {
            "online": r.online,
            "error": r.error,
            "power_kw": r.power_kw,
            "relay": r.relay,
            "temperature_c": r.temperature_c,
            "energy_since_boot_kwh": r.energy_since_boot_kwh,
            "boot_id": r.boot_id,
            "uptime_s": r.uptime_s,
        }

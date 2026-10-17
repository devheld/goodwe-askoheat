from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pymodbus.client import ModbusTcpClient

logger = logging.getLogger(__name__)

# Register map for the GoodWe AC EV Charger 2nd generation (GW11K-HCA and
# siblings), read over Modbus TCP with device id 247.
#
# IMPORTANT - this map is NOT vendor-documented. It comes from evcc's
# charger/goodwe.go, which in turn derives it from the "AC EV Charger 2nd Gen
# Modbus Protocol v1.0.15" via the gen2-modbus branch of the
# pedrodivisez/goodwe-wallbox-sems-home-assistant integration. Every address
# below that this installation could confirm against live values is marked
# "verified"; the rest are taken on trust and may be wrong.
REG_VOLTAGES = 10009  # 3 x U16, 0.1 V (L1/L2/L3) - verified (234.6/238.8/237.5 V)
REG_CURRENTS = 10012  # 3 x U16, 0.1 A (L1/L2/L3)
REG_ACTUAL_POWER = 10015  # U16, 0.1 kW
REG_STATUS = 10017  # U16 enum, see STATUS_TEXT - verified (0 while unplugged)
REG_PHASE_SW_ENABLED = 10023  # U16, 0=off 1=on - verified (1 on this box)
REG_MAX_POWER = 10029  # U16, 0.1 kW, documented raw range [14, 220]
REG_CHARGE_MODE = 10032  # U16, 0=fast 1=PV 2=PV+battery - verified (1 = PV)
REG_POWER_SPEC = 10058  # U16, 0=7kW 1=11kW 2=22kW - verified (2 = 22 kW)
REG_PHASE_SPEC = 10059  # U16, 0=3-phase 1=1-phase - verified (0 = 3-phase)
REG_CHARGE_COMMAND = 10060  # U16, 1=stop 2=start - NEVER WRITTEN HERE, see below
REG_TOTAL_ENERGY = 10065  # U32 (2 regs), 0.1 kWh
REG_RFID = 10500  # 7 regs ASCII, 14-byte card UID - verified (all "0" = no card)

# Addresses that answer but have no known meaning. They are surfaced on the
# dashboard as raw numbers rather than guessed at: watching them change while
# a car charges is the cheapest way to find the alarm/error register, which
# the community map does not contain at all.
DIAGNOSTIC_REGISTERS = (10016, 10018, 10026, 10027, 10030, 10050, 10173, 10175)

# Reads are batched into these blocks to keep the number of Modbus
# round-trips down. 10090..10169 answers "illegal data address" on this
# hardware, so no block may span it.
_BLOCK_MAIN = (10009, 24)  # 10009..10032
_BLOCK_SPEC = (10050, 20)  # 10050..10069
_BLOCK_DIAG = (10170, 10)  # 10170..10179
_BLOCK_RFID = (10500, 7)

STATUS_TEXT = {
    0: "No vehicle connected",
    1: "Vehicle plugged in",
    2: "Handshaking",
    3: "Charging",
    4: "Charging completed",
    5: "Scheduled",
    6: "Scheduled",
    10: "Charging interrupted",
}

# Statuses that mean a car is physically connected (anything but 0 that we
# know the meaning of). Used for the dashboard's "vehicle connected" line.
_PLUGGED_STATUSES = frozenset({1, 2, 3, 4, 5, 6, 10})

CHARGE_MODE_TEXT = {0: "Fast", 1: "PV surplus", 2: "PV + battery"}
POWER_SPEC_TEXT = {0: "7 kW", 1: "11 kW", 2: "22 kW"}


@dataclass(frozen=True)
class WallboxReadings:
    """A single snapshot. Every field is optional: the wallbox is a nice-to-
    have on this dashboard, and a wallbox that is offline, busy with its two
    allowed sessions, or answering only part of its address space must never
    take the heating control loop down with it."""

    status_code: int | None = None
    status_text: str = "Unknown"
    vehicle_connected: bool | None = None
    charging: bool | None = None
    power_kw: float | None = None
    currents_a: tuple[float, float, float] | None = None
    voltages_v: tuple[float, float, float] | None = None
    total_energy_kwh: float | None = None
    charge_mode: str | None = None
    max_power_kw: float | None = None
    power_spec: str | None = None
    phases: str | None = None
    phase_switching: bool | None = None
    rfid_card: str | None = None
    diagnostics: dict[str, int] = field(default_factory=dict)
    online: bool = False
    error: str | None = None


class GoodWeWallboxReader:
    """Read-only Modbus TCP client for the GoodWe AC EV charger.

    Two things about this device shape the design, and both are worth knowing
    before changing anything here:

    1. The wallbox accepts only TWO concurrent Modbus TCP sessions, and the
       SEMS cloud connection normally occupies one of them. That leaves a
       single slot, so this class holds ONE connection open for the process
       lifetime instead of connecting per poll.

    2. Opening a Modbus connection reportedly resets register 10060 (the
       charge start/stop command) to 0, which aborts an ongoing charge - see
       evcc discussion #30669. Reconnecting on every poll would therefore
       interrupt charging once a minute. Keeping the connection open avoids
       that, and it is the reason _read() reconnects only after an actual
       failure rather than opening and closing around each read.

    Nothing in this class writes. Control of the wallbox (charge command, max
    power, charge mode) is deliberately out of scope - the registers are
    known but unverified, and getting them wrong means a car that does not
    charge overnight.
    """

    def __init__(self, host: str, port: int = 502, slave_id: int = 247) -> None:
        self._host = host
        self._port = port
        self._slave_id = slave_id
        self._client: ModbusTcpClient | None = None

    def _ensure_client(self) -> ModbusTcpClient:
        if self._client is None:
            self._client = ModbusTcpClient(self._host, port=self._port, timeout=3)
        if not self._client.connected:
            self._client.connect()
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _read_block(self, client: ModbusTcpClient, start: int, count: int) -> list[int] | None:
        result = client.read_holding_registers(start, count=count, device_id=self._slave_id)
        if result.isError():
            logger.debug("Wallbox block %s(+%s) returned %s", start, count, result)
            return None
        return list(result.registers)

    def read(self) -> WallboxReadings:
        """Reads a snapshot. Never raises - a failure comes back as
        WallboxReadings(online=False, error=...) so the caller can show it on
        the dashboard and carry on."""
        try:
            client = self._ensure_client()
            if not client.connected:
                return WallboxReadings(error="Not reachable")

            main = self._read_block(client, *_BLOCK_MAIN)
            spec = self._read_block(client, *_BLOCK_SPEC)
            diag = self._read_block(client, *_BLOCK_DIAG)
            rfid = self._read_block(client, *_BLOCK_RFID)
        except Exception as exc:
            # Drop the client so the next poll reconnects - but only here,
            # after a real failure, never as part of a normal read.
            logger.warning("Failed to read the wallbox: %s", exc)
            self.close()
            return WallboxReadings(error=str(exc))

        if main is None:
            return WallboxReadings(error="No answer from the wallbox")

        return self._decode(main, spec, diag, rfid)

    def _decode(
        self,
        main: list[int],
        spec: list[int] | None,
        diag: list[int] | None,
        rfid: list[int] | None,
    ) -> WallboxReadings:
        by_addr: dict[int, int] = {}
        for start, block in (
            (_BLOCK_MAIN[0], main),
            (_BLOCK_SPEC[0], spec),
            (_BLOCK_DIAG[0], diag),
        ):
            if block:
                by_addr.update({start + i: v for i, v in enumerate(block)})

        def reg(addr: int) -> int | None:
            return by_addr.get(addr)

        def scaled(addr: int, factor: float, digits: int = 1) -> float | None:
            raw = reg(addr)
            return None if raw is None else round(raw * factor, digits)

        def triple(base: int, factor: float) -> tuple[float, float, float] | None:
            vals = [reg(base + i) for i in range(3)]
            if any(v is None for v in vals):
                return None
            return tuple(round(v * factor, 1) for v in vals)  # type: ignore[return-value]

        status_code = reg(REG_STATUS)
        status_text = (
            STATUS_TEXT.get(status_code, f"Unknown status ({status_code})")
            if status_code is not None
            else "Unknown"
        )

        total_energy = None
        high, low = reg(REG_TOTAL_ENERGY), reg(REG_TOTAL_ENERGY + 1)
        if high is not None and low is not None:
            total_energy = round(((high << 16) | low) * 0.1, 1)

        # Documented raw range is [14, 220]; anything outside it means the
        # address is wrong or the value is not populated while idle, so it is
        # reported as unknown rather than as a bogus 0 kW limit.
        max_power = None
        raw_max = reg(REG_MAX_POWER)
        if raw_max is not None and 14 <= raw_max <= 220:
            max_power = round(raw_max * 0.1, 1)

        card = None
        if rfid:
            text = b"".join(r.to_bytes(2, "big") for r in rfid).decode("ascii", "ignore")
            text = text.strip("\x00 ").strip()
            # An empty slot reads back as all ASCII zeroes rather than nulls.
            card = text if text and set(text) != {"0"} else None

        phase_spec = reg(REG_PHASE_SPEC)
        power_spec = reg(REG_POWER_SPEC)
        charge_mode = reg(REG_CHARGE_MODE)
        phase_sw = reg(REG_PHASE_SW_ENABLED)

        return WallboxReadings(
            status_code=status_code,
            status_text=status_text,
            vehicle_connected=None if status_code is None else status_code in _PLUGGED_STATUSES,
            charging=None if status_code is None else status_code == 3,
            power_kw=scaled(REG_ACTUAL_POWER, 0.1),
            currents_a=triple(REG_CURRENTS, 0.1),
            voltages_v=triple(REG_VOLTAGES, 0.1),
            total_energy_kwh=total_energy,
            charge_mode=CHARGE_MODE_TEXT.get(charge_mode) if charge_mode is not None else None,
            max_power_kw=max_power,
            power_spec=POWER_SPEC_TEXT.get(power_spec) if power_spec is not None else None,
            phases=None if phase_spec is None else ("1-phase" if phase_spec else "3-phase"),
            phase_switching=None if phase_sw is None else bool(phase_sw),
            rfid_card=card,
            diagnostics={
                str(addr): by_addr[addr] for addr in DIAGNOSTIC_REGISTERS if addr in by_addr
            },
            online=True,
        )

    def as_dict(self) -> dict:
        """Snapshot shaped for /api/live and the dashboard tiles."""
        r = self.read()
        return {
            "online": r.online,
            "error": r.error,
            "status_code": r.status_code,
            "status_text": r.status_text,
            "vehicle_connected": r.vehicle_connected,
            "charging": r.charging,
            "power_kw": r.power_kw,
            "currents_a": list(r.currents_a) if r.currents_a else None,
            "voltages_v": list(r.voltages_v) if r.voltages_v else None,
            "total_energy_kwh": r.total_energy_kwh,
            "charge_mode": r.charge_mode,
            "max_power_kw": r.max_power_kw,
            "power_spec": r.power_spec,
            "phases": r.phases,
            "phase_switching": r.phase_switching,
            "rfid_card": r.rfid_card,
            "diagnostics": r.diagnostics,
        }

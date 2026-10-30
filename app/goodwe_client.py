from __future__ import annotations

import logging
from dataclasses import dataclass

from .modbus_link import ModbusLink

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Readings:
    pv_kw: float
    consumption_kw: float
    battery_charge_kw: float
    battery_discharge_kw: float
    grid_kw: float
    # True while the battery is NOT draining - idle counts as charging, see
    # the note on the dead band in GoodWeModbusReader. The control loop wants
    # exactly that question answered; anything that TELLS the user what the
    # battery is doing has to use battery_state instead.
    battery_charging: bool
    # "charging" | "idle" | "discharging" - the same measurement as the two
    # power fields above, reduced to the three states worth putting in a
    # sentence.
    battery_state: str
    # BMS state of charge in percent, or None when the SOC read failed - the
    # controller treats an unknown SOC as "battery not full" (see
    # SurplusController._tick), which keeps the battery's priority.
    battery_soc_pct: float | None
    # The house load split over the three phases, from the same register
    # block as consumption_kw and summing to it. Recorded because the
    # standing base load of this house sits almost entirely on L3, and the
    # only other instrument that can show that is a clamp meter which may
    # not stay at the connection point. None when the block came up short.
    load_phase_kw: tuple[float, float, float] | None = None


class GoodWeModbusReader:
    """Reads PV/consumption/battery/grid directly from the inverter over
    local Modbus TCP, using the official GoodWe ET-series register map.

    Local access requires GoodWe support to enable Modbus/TCP on the data
    logger - by default only the (mostly broken, see git history of this
    file) SEMS cloud API and a DTLS-locked UDP protocol are available. Once
    enabled, this gives real measured values instead of the various
    workarounds the cloud-based approach needed:
      - pv_kw: real Ppv1..Ppv4 (register 35105 etc.), one register block per
        MPPT string, summed. Unused strings read 0W and don't contribute.
      - consumption_kw: register 35171 "Total Load power" - the inverter's
        own measured on-grid load, not an approximation.
      - grid_kw: register 35139 "AC ActivePower" - falls back to the
        inverter's own on-grid port measurement when no external smart meter
        is connected (which is the case on this installation), so this is
        the real grid import/export power, not a fixed 0. Positive means
        export (feed-in), negative means import - confirmed on recorded
        history, where pv - battery - load equals this value exactly on
        every row. SurplusController relies on that sign convention once
        the battery is full and the surplus goes to the grid.
      - battery_charge_kw/battery_discharge_kw: register 35182 "Pbattery1"
        (signed), split into two always-non-negative values for display -
        real measured battery power, no derivation or bias-correction
        margin needed anymore (see the old GoodWeCloudReader for what that
        looked like). Pbattery1 is negative while charging and positive
        while discharging (the opposite of the intuitive convention) -
        confirmed by checking that PV - load - grid - Pbattery1 balances to
        ~0 on live data, whereas without the sign flip it was off by several
        kW.
      - battery_soc_pct: register 37007 "SOC" from the BMS information
        block. That block is far outside the 35103+ block below, so it needs
        its own request - see _read_soc_pct(), which deliberately degrades to
        None instead of failing the whole read.

    battery_charging still applies zero_threshold_kw as a tolerance around 0
    (not a hard >= 0 cut): Ibattery1's resolution is 0.1A, which at this
    installation's ~765V battery bus is ~76W per step, so idle noise alone
    can read as a few hundredths of a kW of "discharge" with no tolerance.
    That tolerance is right for the control decision and wrong for a status
    text, which is why battery_state exists next to it: a battery sitting at
    -4W is inside the dead band, so battery_charging is true, and the
    dashboard used to report that as "charging".

    The registers are read in a single request covering 35103-35184
    (well within the ~125 register limit this device accepts per request).
    All multi-register values use big-endian (high word first) encoding,
    confirmed by comparing Vbattery1 x Ibattery1 against the reported
    Pbattery1 (magnitude, before the sign flip above was known).

    The device always responds with unit/device ID 247 regardless of what
    is requested, so device_id must be set to 247 - requesting with a
    different ID gets the response silently dropped by pymodbus.

    Like AskoHeatController, the connection is a ModbusLink: it can be reset
    by the remote host, and the inverter can also be off the network
    entirely, which must not stop the service from starting.
    """

    BASE_REGISTER = 35103
    READ_COUNT = 82  # covers up to 35184 (Battery1 Mode), plenty of margin

    # BMS information block ("SOC", first group battery capacity, in %).
    SOC_REGISTER = 37007

    def __init__(
        self,
        host: str,
        port: int = 502,
        slave_id: int = 247,
        zero_threshold_kw: float = 0.01,
    ) -> None:
        self._host = host
        self._port = port
        self._slave_id = slave_id
        self._zero_threshold_kw = zero_threshold_kw
        self._link = ModbusLink("GoodWe inverter", host, port)

    def connect(self) -> None:
        self._link.connect()

    def close(self) -> None:
        self._link.close()

    @property
    def online(self) -> bool:
        return self._link.online

    def read(self) -> Readings:
        def _do() -> list[int]:
            result = self._link.client.read_holding_registers(
                self.BASE_REGISTER, count=self.READ_COUNT, device_id=self._slave_id
            )
            if result.isError():
                raise IOError(f"GoodWe Modbus read returned an error: {result}")
            return result.registers

        regs = self._link.call("read", _do)
        return self._decode(regs, self._read_soc_pct())

    def _read_soc_pct(self) -> float | None:
        """reads the BMS state of charge (register 37007), which sits well
        outside the main 35103+ block and therefore needs its own request.

        Returns None rather than raising when the read fails or the value is
        implausible: the controller reads an unknown SOC as "battery not
        full" and leaves the heater off, so a failing SOC read costs heating
        time but never overrides the battery's priority - whereas letting it
        propagate would drop the whole tick, including the power readings
        that are perfectly fine.
        """

        def _do() -> int:
            result = self._link.client.read_holding_registers(
                self.SOC_REGISTER, count=1, device_id=self._slave_id
            )
            if result.isError():
                raise IOError(f"GoodWe SOC read returned an error: {result}")
            return result.registers[0]

        try:
            soc = float(self._link.call("SOC read", _do))
        except Exception:
            logger.exception("Failed to read the battery SOC")
            return None

        if not 0 <= soc <= 100:
            logger.warning("GoodWe reported an implausible SOC of %s%% - ignoring", soc)
            return None
        return soc

    def _decode(self, regs: list[int], battery_soc_pct: float | None) -> Readings:
        def offset(addr: int) -> int:
            return addr - self.BASE_REGISTER

        def u32(addr: int) -> int:
            i = offset(addr)
            return (regs[i] << 16) | regs[i + 1]

        def s32(addr: int) -> int:
            v = u32(addr)
            return v - 0x100000000 if v >= 0x80000000 else v

        pv_kw = sum(u32(addr) for addr in (35105, 35109, 35113, 35117)) / 1000
        consumption_kw = s32(35171) / 1000
        # 35163/35165/35167 are the per-phase load registers of the same
        # block. Their sum matches 35171 to within a watt (2885 against 2886
        # when this was checked). Read signed, because a phase can carry a
        # reverse flow even while the total is positive.
        load_phase_kw = tuple(
            round(s32(addr) / 1000, 3) for addr in (35163, 35165, 35167)
        )
        grid_kw = s32(35139) / 1000
        # Pbattery1 is negative while charging and positive while discharging
        # (opposite of the intuitive convention) - confirmed by comparing
        # against the PV/load/grid energy balance on live data. Negate so
        # positive here means charging, matching battery_charge_kw below.
        battery_signed_kw = -s32(35182) / 1000

        battery_charge_kw = max(battery_signed_kw, 0.0)
        battery_discharge_kw = max(-battery_signed_kw, 0.0)
        battery_charging = battery_signed_kw >= -self._zero_threshold_kw
        # The same dead band, but applied on both sides, so "doing nothing"
        # is its own state instead of being folded into "charging".
        if battery_signed_kw > self._zero_threshold_kw:
            battery_state = "charging"
        elif battery_signed_kw < -self._zero_threshold_kw:
            battery_state = "discharging"
        else:
            battery_state = "idle"

        return Readings(
            pv_kw=pv_kw,
            consumption_kw=consumption_kw,
            battery_charge_kw=battery_charge_kw,
            battery_discharge_kw=battery_discharge_kw,
            grid_kw=grid_kw,
            battery_charging=battery_charging,
            battery_state=battery_state,
            battery_soc_pct=battery_soc_pct,
            load_phase_kw=load_phase_kw,
        )

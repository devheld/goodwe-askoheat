from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from pymodbus.client import ModbusTcpClient

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class Readings:
    pv_kw: float
    consumption_kw: float
    battery_charge_kw: float
    battery_discharge_kw: float
    grid_kw: float
    battery_charging: bool
    # BMS state of charge in percent, or None when the SOC read failed - the
    # controller treats an unknown SOC as "battery not full" (see
    # SurplusController._tick), which keeps the battery's priority.
    battery_soc_pct: float | None


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

    The registers are read in a single request covering 35103-35184
    (well within the ~125 register limit this device accepts per request).
    All multi-register values use big-endian (high word first) encoding,
    confirmed by comparing Vbattery1 x Ibattery1 against the reported
    Pbattery1 (magnitude, before the sign flip above was known).

    The device always responds with unit/device ID 247 regardless of what
    is requested, so device_id must be set to 247 - requesting with a
    different ID gets the response silently dropped by pymodbus.

    Like AskoHeatController, every read goes through a reconnect-and-retry
    wrapper since the TCP connection can be reset by the remote host.
    """

    BASE_REGISTER = 35103
    READ_COUNT = 82  # covers up to 35184 (Battery1 Mode), plenty of margin

    # BMS information block ("SOC", first group battery capacity, in %).
    SOC_REGISTER = 37007

    MAX_RETRIES = 2
    RETRY_DELAY_S = 1

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
        self._client = ModbusTcpClient(host, port=port)

    def connect(self) -> None:
        if not self._client.connect():
            raise ConnectionError(f"Could not reach GoodWe inverter at {self._host}:{self._port}")

    def close(self) -> None:
        self._client.close()

    def _reconnect(self) -> None:
        logger.warning("Reconnecting to GoodWe inverter at %s:%s", self._host, self._port)
        try:
            self._client.close()
        except Exception:
            logger.exception("Error while closing the old GoodWe connection (ignoring)")
        if not self._client.connect():
            raise ConnectionError(f"Could not reach GoodWe inverter at {self._host}:{self._port}")

    def _with_retry(self, operation_name: str, func: Callable[[], T]) -> T:
        last_exc: Exception = RuntimeError("unreachable")
        for attempt in range(1, self.MAX_RETRIES + 2):
            try:
                return func()
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "GoodWe %s failed (attempt %s/%s): %s",
                    operation_name,
                    attempt,
                    self.MAX_RETRIES + 1,
                    exc,
                )
                if attempt <= self.MAX_RETRIES:
                    time.sleep(self.RETRY_DELAY_S)
                    try:
                        self._reconnect()
                    except Exception:
                        logger.exception("Reconnect to GoodWe inverter failed")
        raise last_exc

    def read(self) -> Readings:
        def _do() -> list[int]:
            result = self._client.read_holding_registers(
                self.BASE_REGISTER, count=self.READ_COUNT, device_id=self._slave_id
            )
            if result.isError():
                raise IOError(f"GoodWe Modbus read returned an error: {result}")
            return result.registers

        regs = self._with_retry("read", _do)
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
            result = self._client.read_holding_registers(
                self.SOC_REGISTER, count=1, device_id=self._slave_id
            )
            if result.isError():
                raise IOError(f"GoodWe SOC read returned an error: {result}")
            return result.registers[0]

        try:
            soc = float(self._with_retry("SOC read", _do))
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
        grid_kw = s32(35139) / 1000
        # Pbattery1 is negative while charging and positive while discharging
        # (opposite of the intuitive convention) - confirmed by comparing
        # against the PV/load/grid energy balance on live data. Negate so
        # positive here means charging, matching battery_charge_kw below.
        battery_signed_kw = -s32(35182) / 1000

        battery_charge_kw = max(battery_signed_kw, 0.0)
        battery_discharge_kw = max(-battery_signed_kw, 0.0)
        battery_charging = battery_signed_kw >= -self._zero_threshold_kw

        return Readings(
            pv_kw=pv_kw,
            consumption_kw=consumption_kw,
            battery_charge_kw=battery_charge_kw,
            battery_discharge_kw=battery_discharge_kw,
            grid_kw=grid_kw,
            battery_charging=battery_charging,
            battery_soc_pct=battery_soc_pct,
        )

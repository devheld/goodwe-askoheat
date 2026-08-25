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
        the real grid import/export power, not a fixed 0.
      - battery_charge_kw/battery_discharge_kw: register 35182 "Pbattery1"
        (signed), split into two always-non-negative values for display -
        real measured battery power, no derivation or bias-correction
        margin needed anymore (see the old GoodWeCloudReader for what that
        looked like). Pbattery1 is negative while charging and positive
        while discharging (the opposite of the intuitive convention) -
        confirmed by checking that PV - load - grid - Pbattery1 balances to
        ~0 on live data, whereas without the sign flip it was off by several
        kW.

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

    MAX_RETRIES = 2
    RETRY_DELAY_S = 1

    def __init__(
        self,
        host: str,
        port: int = 502,
        slave_id: int = 247,
    ) -> None:
        self._host = host
        self._port = port
        self._slave_id = slave_id
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
        def _do() -> Readings:
            result = self._client.read_holding_registers(
                self.BASE_REGISTER, count=self.READ_COUNT, device_id=self._slave_id
            )
            if result.isError():
                raise IOError(f"GoodWe Modbus read returned an error: {result}")
            return self._decode(result.registers)

        return self._with_retry("read", _do)

    def _decode(self, regs: list[int]) -> Readings:
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
        battery_charging = battery_signed_kw >= 0

        return Readings(
            pv_kw=pv_kw,
            consumption_kw=consumption_kw,
            battery_charge_kw=battery_charge_kw,
            battery_discharge_kw=battery_discharge_kw,
            grid_kw=grid_kw,
            battery_charging=battery_charging,
        )

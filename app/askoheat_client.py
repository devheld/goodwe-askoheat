from __future__ import annotations

import struct
import time

from pymodbus.client import ModbusTcpClient


class AskoHeatController:
    """Writes/reads the AskoHeat's heater step (0-7) and reads its
    temperature over Modbus TCP.

    Register addresses per the official AskoHeat+ Modbus documentation:
      - 200 MODBUS_CMD_SET_HEATER_STEP (Command Block, 200-202): byte, 0-7,
        R/W. Bitmask of the 3 heating element relays (bit0=heater1,
        bit1=heater2, bit2=heater3), giving 7 power steps in combination
        (0 = all off).
      - 110 MODBUS_IREG_HEATER_LOAD: uint16, 250-30000W, read-only (input
        register, function 4), currently measured electrical heating load.
      - 325 MODBUS_EMA_TEMPERATURE_FLOAT_SENSOR0 (Energymanager block, 300+):
        float32 big-endian, degrees Celsius, read-only (input register,
        function 4 - despite the documentation saying otherwise, this block
        only responds to function 4 on this installation, not 3). The other
        sensors 1-4 (registers 327/329/331/333) are not wired up on this
        installation (constantly report the sentinel value 9999.9).
    """

    def __init__(
        self,
        host: str,
        port: int,
        step_register: int,
        load_register: int,
        temp_register: int,
        slave_id: int,
        read_delay_s: int,
    ) -> None:
        self._host = host
        self._port = port
        self._step_register = step_register
        self._load_register = load_register
        self._temp_register = temp_register
        self._slave_id = slave_id
        self._read_delay_s = read_delay_s
        self._client = ModbusTcpClient(host, port=port)

    def connect(self) -> None:
        if not self._client.connect():
            raise ConnectionError(f"Could not reach AskoHeat at {self._host}:{self._port}")

    def close(self) -> None:
        self._client.close()

    def set_step(self, step: int) -> int:
        """writes the heater step (0-7) and reads back the current heating load"""
        self._client.write_register(self._step_register, step, device_id=self._slave_id)
        time.sleep(self._read_delay_s)
        result = self._client.read_input_registers(self._load_register, device_id=self._slave_id)
        return result.registers[0]

    def read_temperature_c(self) -> float:
        """reads the tank temperature (sensor 0) in degrees Celsius"""
        result = self._client.read_input_registers(
            self._temp_register, count=2, device_id=self._slave_id
        )
        raw = struct.pack(">HH", result.registers[0], result.registers[1])
        return struct.unpack(">f", raw)[0]

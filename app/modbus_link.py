from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

from pymodbus.client import ModbusTcpClient

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ModbusLink:
    """A Modbus TCP connection that puts itself back together.

    Both permanently connected devices here drop the connection on their own:
    the AskoHeat resets an idle socket, and the inverter's data logger
    reconnects to the cloud on its own schedule. pymodbus does not recover
    from either - the next call just raises a raw socket error - so every
    operation goes through call(), which reconnects and retries.

    Beyond that it exists so that a device being switched off, unplugged or
    simply not there yet is an ordinary condition rather than an outage:

      - Nothing here blocks waiting for a device. connect() reports whether
        it worked and the caller decides; a device that never answered is
        picked up by the first call() that succeeds.
      - A device that has just failed is treated as down, and while it is,
        each call gets a single reconnect-and-try instead of three. Attempts
        are not free - each one pays a full TCP connect timeout, and with
        three attempts across two operations a missing heater cost about 25
        seconds of every 60 second poll. Recovery is just as quick either
        way, because the reconnect still happens on every call; it simply
        stops costing most of a poll interval to find out that something is
        still missing.
    """

    # Retries per operation, on top of the first try, while the device is
    # considered healthy.
    MAX_RETRIES = 2
    RETRY_DELAY_S = 1

    def __init__(self, name: str, host: str, port: int, timeout: float | None = None) -> None:
        self._name = name
        self._host = host
        self._port = port
        kwargs = {} if timeout is None else {"timeout": timeout}
        self._client = ModbusTcpClient(host, port=port, **kwargs)
        # Starts degraded: nothing has been read yet, so the first call
        # reconnects rather than assuming a socket that may never have been
        # opened. connect() clears it when the device answers at startup.
        self._degraded = True

    @property
    def client(self) -> ModbusTcpClient:
        return self._client

    @property
    def online(self) -> bool:
        """Whether the last operation succeeded. Not a probe - it says what
        happened, it does not go and ask."""
        return not self._degraded

    def connect(self) -> None:
        """Open the connection, raising if the device does not answer.

        Callers that can work without the device should catch this and carry
        on; call() re-establishes the connection on its own either way.
        """
        if not self._client.connect():
            raise ConnectionError(f"Could not reach {self._name} at {self._host}:{self._port}")
        self._degraded = False

    def close(self) -> None:
        self._client.close()

    def reconnect(self) -> None:
        logger.warning("Reconnecting to %s at %s:%s", self._name, self._host, self._port)
        try:
            self._client.close()
        except Exception:
            logger.exception(
                "Error while closing the old %s connection (ignoring)", self._name
            )
        if not self._client.connect():
            raise ConnectionError(f"Could not reach {self._name} at {self._host}:{self._port}")

    def call(self, operation_name: str, func: Callable[[], T]) -> T:
        """Run a Modbus operation, reconnecting and retrying as needed."""
        attempts = 1 if self._degraded else self.MAX_RETRIES + 1
        last_exc: Exception = RuntimeError("unreachable")

        for attempt in range(1, attempts + 1):
            # Reconnect before a retry, and before the only attempt when the
            # device is already known to be down - otherwise a single-attempt
            # call would keep using a socket that is known to be dead and
            # could never recover.
            if attempt > 1 or self._degraded:
                try:
                    self.reconnect()
                except Exception as exc:
                    last_exc = exc
                    logger.warning("Reconnect to %s failed: %s", self._name, exc)
                    if attempt < attempts:
                        time.sleep(self.RETRY_DELAY_S)
                    continue
            try:
                value = func()
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "%s %s failed (attempt %s/%s): %s",
                    self._name,
                    operation_name,
                    attempt,
                    attempts,
                    exc,
                )
                if attempt < attempts:
                    time.sleep(self.RETRY_DELAY_S)
                continue
            self._degraded = False
            return value

        self._degraded = True
        raise last_exc

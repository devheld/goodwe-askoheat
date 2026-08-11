from __future__ import annotations

from dataclasses import dataclass

from pygoodwe import API


@dataclass(frozen=True)
class Readings:
    pv_kw: float
    consumption_kw: float
    battery_charge_kw: float
    battery_discharge_kw: float
    grid_kw: float
    battery_charging: bool


class GoodWeCloudReader:
    """Reads PV/consumption/battery/grid via the GoodWe SEMS cloud API.

    On this installation the cloud API's "powerflow" block is largely
    unusable, so the actual values are derived from raw inverter registers:
      - powerflow["pv"] is useless (no smart meter, reported power factor is
        ~0 so the AC-side power calculation collapses to ~0W). The DC-side
        MPPT readings (voltage x current per string) give the real PV
        production instead.
      - powerflow["load"] inherits the same bug and is derived from the
        inverter's AC output instead (vac x iac per phase, power factor
        ignored). On this DC-coupled hybrid inverter, PV and the battery both
        sit BEFORE this AC stage, so the AC output is automatically already
        consumption + grid export, without needing a (broken) battery value.
      - powerflow["bettery"] reports a constant 0 regardless of the actual
        state. Battery charge/discharge is therefore derived indirectly from
        the energy balance PV - consumption - grid, and split into two
        always-non-negative values (battery_charge_kw/battery_discharge_kw)
        instead of one signed value - simpler for display/charting.
    """

    def __init__(self, account: str, password: str, station_id: str) -> None:
        # skipload=True: otherwise the API constructor would immediately load
        # data synchronously (with built-in retries and sys.exit() on final
        # failure). That must not happen here, since GoodWeCloudReader is
        # instantiated on the main thread during FastAPI startup - a network
        # hiccup would otherwise block or kill the whole server. The first
        # real fetch only happens in read(), which runs in the
        # SurplusController's background thread and handles errors.
        self._api = API(system_id=station_id, account=account, password=password, skipload=True)

    @staticmethod
    def _to_kw(value: str) -> float:
        """converts a powerflow string like '1100(W)' into kW"""
        if value.endswith("(W)"):
            value = value[:-3]
        return float(value) / 1000

    def read(self) -> Readings:
        data = self._api.get_current_readings()
        powerflow = data["powerflow"]
        inverter_full = data["inverter"][0]["invert_full"]

        pv_kw = sum(
            inverter_full.get(f"vpv{i}", 0.0) * inverter_full.get(f"ipv{i}", 0.0)
            for i in range(1, 5)
        ) / 1000

        grid_kw = self._to_kw(powerflow["grid"])

        ac_output_kw = sum(
            inverter_full.get(f"vac{i}", 0.0) * inverter_full.get(f"iac{i}", 0.0)
            for i in range(1, 4)
        ) / 1000
        consumption_kw = ac_output_kw - grid_kw

        battery_signed_kw = pv_kw - consumption_kw - grid_kw
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

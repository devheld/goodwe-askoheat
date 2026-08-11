# AskoHeat Manager

A small self-hosted controller and web dashboard that diverts excess solar
power into a resistive water heater (an [Askoma AskoHeat+](https://www.askoma.com))
instead of letting it go to waste, based on live data from a GoodWe hybrid
inverter and battery system.

It reads PV production, household consumption, battery charge/discharge and
grid flow from the GoodWe SEMS cloud API, decides whether there is genuine
surplus power available, and steps the AskoHeat's heating elements up or
down (0-7) via Modbus TCP accordingly. A FastAPI backend keeps a history in
SQLite and serves a live/history dashboard.

## Why this exists

The grid connection this system runs on currently permits **0% feed-in** -
no export to the grid is allowed at all right now. That means any PV surplus
that isn't consumed immediately or stored in the battery would simply be
curtailed and wasted by the inverter.

Diverting that surplus into the AskoHeat's resistive heating element is less
efficient than using the home's heat pump for the same amount of hot water
(resistive heating is roughly 1:1, a heat pump's coefficient of performance
is more like 3-4:1) - but energy that would otherwise be thrown away entirely
is still better spent as 1:1 heat than not used at all. As a side effect, it
also reduces cycling on the heat pump, since part of the domestic hot water
demand is now covered by the AskoHeat instead.

If the feed-in restriction is ever lifted, this trade-off should be
revisited - grid export may then become preferable to resistive heating.

## Architecture

- `app/goodwe_client.py` - `GoodWeCloudReader`: reads PV/consumption/battery/
  grid via the GoodWe SEMS cloud API
- `app/askoheat_client.py` - `AskoHeatController`: sets the AskoHeat's heater
  step and reads back load/temperature over Modbus TCP
- `app/controller.py` - `SurplusController`: the core control loop - on every
  poll, fetches GoodWe data, decides whether there is surplus, steps the
  AskoHeat up or down, and records the result
- `app/database.py` - `HistoryStore`: SQLite persistence for the dashboard's
  history charts
- `app/main.py` - FastAPI app serving `/api/live`, `/api/history`, and the
  static dashboard in `app/static/`

The control loop runs on a background thread rather than as an asyncio task,
because the GoodWe and Modbus client libraries are both synchronous and the
AskoHeat write includes a several-second wait for the device to respond -
running that on the main event loop would block the whole web server.

## Problems encountered along the way

### 1. GoodWe's cloud API reports broken values

The standard `powerflow` block returned by the SEMS cloud API
(`GetMonitorDetailByPowerstationId`) reports PV, load and battery power as
essentially always ~0W on this installation, regardless of the real state.
Root cause: a bogus reported power factor (~0.02-0.05) that collapses every
AC-side power calculation to near zero. There is also no smart meter
installed (`hasmeter: false`), so `load`/`grid` are internally just derived
from the (already broken) PV value.

**Workaround:**
- PV production is computed from the DC-side MPPT string registers
  (`vpv{n} x ipv{n}`), which are unaffected by the power factor bug.
- Consumption is computed from the inverter's AC output (`vac{n} x iac{n}`,
  ignoring the broken power factor - i.e. assuming close to unity power
  factor, a reasonable assumption for normal grid-tied operation). Because
  PV and the battery both sit on the DC bus *before* this AC stage on this
  hybrid inverter, the AC output already nets out battery charge/discharge
  automatically - no separate battery reading is required for this value.
- Battery charge/discharge isn't reported at all: the value is constantly 0
  (confirmed identical on both the v2 and v3 SEMS API endpoints, and even
  the raw `vbattery1`/`ibattery1` inverter registers read 0 despite real,
  confirmed charging/discharging happening at the time). It's therefore
  derived indirectly from the energy balance `PV - consumption - grid`, and
  split into two always-non-negative values (`battery_charge_kw` /
  `battery_discharge_kw`) rather than one signed value, mainly to keep the
  dashboard simple.

### 2. Local access instead of the cloud didn't work out

Before settling on the cloud-based workaround above, local access to the
inverter was attempted using the [`goodwe`](https://github.com/marcelblijleven/goodwe)
Python library (UDP, port 8899) to avoid the cloud entirely. Discovery
worked - a broadcast reveals the dongle's IP and serial number - but every
actual data request timed out. The discovery response explained why:

```
dongle@sn,dtls_port:8899,<serial>
```

Newer GoodWe dongle firmware requires DTLS-encrypted communication on the
data port, which no current open-source tool supports (no published pre-
shared key). Local Modbus TCP (port 502) is open on the dongle but doesn't
respond to standard Modbus requests either. This is left as a known
limitation - see below.

### 3. Finding the right AskoHeat Modbus registers

The AskoHeat+ Modbus register map is documented, but a naive web search
initially returned register numbers from a different, similarly-named
register block than the ones that actually work on this device. The
addresses below were confirmed by cross-referencing the official
documentation against the source code of the open-source
[toggm/askoheat](https://github.com/toggm/askoheat) Home Assistant
integration, and verified live against the real device:

| Register | Name | Type | Access | Notes |
|---|---|---|---|---|
| 200 | `MODBUS_CMD_SET_HEATER_STEP` | byte, 0-7 | R/W | Bitmask of the 3 heater relays; 7 combined power steps |
| 110 | `MODBUS_IREG_HEATER_LOAD` | uint16, W | R (input register) | Actual measured electrical heating load |
| 325 | `MODBUS_EMA_TEMPERATURE_FLOAT_SENSOR0` | float32 big-endian, °C | R (input register) | Tank temperature; despite the documentation, this device only answers via input register (function 4), not holding register |

Temperature sensors 1-4 (registers 327/329/331/333) are unused on this
installation and constantly report the sentinel value `9999.9`.

### 4. Target vs. actual step confusion

The AskoHeat has its own internal ~65°C safety limit and silently cuts power
to the heating elements once the tank is hot enough, regardless of the step
commanded over Modbus. This initially looked like a bug (step showing 7/7
while actual load was 0W). Addressed on the dashboard rather than in the
control logic (the device overriding us here is exactly the safety behaviour
it should have):
- the commanded step and the measured load are clearly labelled and charted
  separately ("Target" vs. "Actual")
- the temperature chart has a dashed reference line at the device's
  temperature limit, so the cause is visible at a glance

### 5. Dashboard updates felt very slow

New readings appeared in the backend log immediately but took a long time to
show up in the browser. Root cause: browsers throttle `setInterval` timers
heavily in background or inactive tabs (Chrome in particular can drop to
about one timer firing per minute or less). This is a browser behaviour, not
a bug in the app - noted here as a known limitation; keep the dashboard tab
focused for near-real-time updates.

## Setup

```
pip install -r requirements.txt
cp .env.example .env   # fill in your SEMS account, station ID and AskoHeat details
uvicorn app.main:app --reload
```

Then open `http://127.0.0.1:8000`.

## Known limitations

- The GoodWe SEMS cloud API only updates every few minutes, not in real
  time - the poll interval is tuned accordingly, so this isn't a truly
  real-time controller.
- No local/real-time access to the inverter is currently possible (see
  problem #2 above) - everything goes through the cloud.
- Register addresses and the value-derivation formulas above are specific to
  this particular installation (GoodWe GW15K-ETA-G20 hybrid inverter +
  AskoHeat+) and firmware version, and may not generalize to other models.
- `.env` holds real credentials and is git-ignored - never commit it.

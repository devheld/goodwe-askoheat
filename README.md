# AskoHeat Manager

A small self-hosted controller and web dashboard that diverts excess solar
power into a resistive water heater (an [Askoma AskoHeat+](https://www.askoma.com))
instead of letting it go to waste, based on live data from a GoodWe hybrid
inverter and battery system.

It reads PV production, household consumption, battery charge/discharge,
state of charge and grid flow from the GoodWe inverter, decides whether
there is genuine surplus power left over once the battery has been charged,
and steps the AskoHeat's heating elements up or down (0-7) via Modbus TCP
accordingly - see [Control logic](#control-logic). A FastAPI backend keeps a history in
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

## Control logic

**The house battery has priority over the water tank.** No surplus is
diverted into the AskoHeat until the battery has reached
`BATTERY_FULL_SOC_PCT` (95% by default, read from the inverter's BMS
register 37007). This matters from autumn onwards: a day's surplus is then
no longer enough to both fill the battery and heat the tank, and stored
energy is worth more than hot water - it still covers the evening load at
1:1, whereas resistive heating cannot be turned back into electricity. In
summer the battery is usually full by midday anyway, so this barely changes
when heating starts.

Once the battery is full, the heater steps up or down by one step per poll:

| Situation | Action |
| --- | --- |
| SOC below the threshold (or unreadable) | step down - battery first |
| Battery discharging, or importing from the grid | step down - the heater is taking more than is available |
| Exporting at least `SURPLUS_STEP_MARGIN_KW` | step up |
| Exporting less than that | hold - the smallest step (~600W) would not fit |
| No grid flow, battery full and idle | step up - see curtailment below |

That last row is the normal case on this installation, which runs at 0%
feed-in: with the battery full, the inverter curtails the PV strings instead
of exporting, so the surplus never shows up in the readings - production
simply tracks the load. The controller therefore adds a step and lets the
inverter release some of the curtailed production to cover it; if there was
nothing left after all, the battery starts discharging and the step is taken
straight back on the next poll.

A running heating cycle is allowed to continue down to
`BATTERY_KEEP_HEATING_SOC_PCT` (90%) before the battery takes priority
again. Without that gap the heater's own load - which is exactly what stops
the battery from charging further - would switch heating on and off around a
single threshold every few minutes. A second, independent hysteresis applies
to the tank: a *new* cycle only starts at or below `HEATING_START_MAX_TEMP_C`,
while a running one is left to the AskoHeat's own ~65°C cutoff.

## Architecture

- `app/goodwe_client.py` - `GoodWeModbusReader`: reads PV/consumption/
  battery/state of charge/grid from the inverter over local Modbus TCP
- `app/askoheat_client.py` - `AskoHeatController`: sets the AskoHeat's heater
  step and reads back load/temperature over Modbus TCP
- `app/controller.py` - `SurplusController`: the core control loop - on every
  poll, fetches GoodWe data, decides whether there is surplus, steps the
  AskoHeat up or down, and records the result
- `app/shelly_client.py` - `ShellyClient`: reads a Shelly 3EM at the grid
  connection point over HTTP - a second, independent meter, and the only
  instrument here that measures the RMS current through the main fuse
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
respond to standard Modbus requests either.

**Resolved afterwards, and this is what the project uses today:** GoodWe
support can enable Modbus/TCP on the data logger on request. Once they did,
the ET-series register map worked directly over port 502 and the cloud
reader was dropped for `GoodWeModbusReader` - real measured values instead
of the derivations and bias corrections the cloud detour needed. The DTLS
problem above still stands for the UDP protocol; it was the Modbus route
that got opened up, not the encrypted one. Note that this is a per-device
change by the manufacturer, not something you can configure yourself.

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

### 6. One missing device took the whole service down

The immersion heater was switched off for an afternoon. The dashboard then
answered `503 Service Unavailable` for an hour while the inverter was
reachable the entire time, and no readings were recorded at all.

`run_forever()` connected to each device before entering its loop, retrying
forever on failure. A heater that was off the network therefore meant the
loop never reached its first tick: nothing was read, nothing was stored, and
`/api/live` had no reading to return. Nothing actually needed that wait -
`_tick()` already handled a heater that stops answering mid-run, and the
Modbus connections reconnect on their own.

What the devices look like now:

- **Nothing is waited for at startup.** Each device is tried once and the
  service starts either way. A device that was not there is picked up by the
  first operation that succeeds.
- **Each connection repairs itself** (`app/modbus_link.py`). This was already
  true for the two permanent connections, since the AskoHeat resets idle
  sockets and the inverter's data logger reconnects to the cloud on its own
  schedule; the charger has always opened and closed a connection per poll.
- **A device known to be down costs one attempt, not three.** Attempts are
  not free - each pays a full TCP connect timeout - and with three attempts
  across two operations a missing heater spent about 25 seconds of every 60
  second poll waiting on a socket that was never going to answer. Recovery is
  no slower for it: the reconnect still happens on every call.
- **A failed inverter read still publishes.** `/api/live` returns the device
  health and a sentence naming what is missing instead of a 503, and the
  dashboard prints which devices are not answering. The measurements are left
  empty rather than carried over from the last good tick - a PV figure from
  twenty minutes ago is worse than none, because nothing on the page would
  say how old it is.
- **The charger is read even when the inverter is not answering.** It is
  separate hardware on a separate connection; it was only being skipped
  because of the order of the lines in `_tick()`.

The one thing still waited for is the inverter *as a data source*: without it
a tick has nothing to measure, so it records nothing and says so.

### 7. Reading the charger was interrupting the charge

A car plugged into the wallbox charged for 3.5 minutes, paused for 2.3, and
repeated for hours. The search for the cause ran through the obvious
suspects first and all of them were wrong: a grid import limit, the house
battery charging from the grid alongside the car, the charger's dynamic load
control, the car's onboard charger overheating. Each one looked plausible,
and each was ruled out by the same observation - the cycle stayed at 5.9
minutes whether the energy came from the grid at 16 kW, from the house
battery at zero grid import, or from PV, and whatever limit was configured.
A regulator reacting to power cannot produce a constant period across that
range.

5.9 minutes is `WALLBOX_POLL_INTERVAL_S=300` rounded up to the 70-second
tick grid. Every pause began one tick after a poll. **Reading any register
over Modbus interrupts a charge on this hardware**: the box drops the
charging current to 0 A while still reporting status 3 ("Charging"), and the
car takes current again about two minutes after the TCP connection closes.
This is documented behaviour for the GW11K-HCA in [evcc discussion
#13970](https://github.com/evcc-io/evcc/discussions/13970), and from the
other side for the G2 in [#30669](https://github.com/evcc-io/evcc/discussions/30669),
where opening a connection at all stops the charge.

The proof was to stop polling: the same car then charged 17.6 minutes
without a break, against a previous best of 3.6 minutes across four days of
recorded data.

`SurplusController._poll_wallbox` now skips the read entirely while a car is
drawing. What counts as drawing is the household load above the heater,
compared against `WALLBOX_BUSY_LOAD_KW` (3.0 kW by default - above a house
base load, below a three-phase car at the 6 A minimum of 4.14 kW). Between
skipped polls the last snapshot is reused and the dashboard says why it is
standing still.

Three things make that gate hold up in practice:

- **It waits before calling a charge over.** Resuming on the first quiet
  tick would read into a pause and stop the car coming back, which is the
  fault itself. The load has to stay low for `WALLBOX_IDLE_TICKS` (5, about
  six minutes), well past the longest pause observed.
- **A reading beats the estimate.** A single-phase car at the 6 A minimum
  draws 1.38 kW and hides under the threshold. If a poll comes back with
  current on any phase, the gate closes again immediately.
- **A car on the cable is read slowly.** Charging or parked, the interval
  becomes `WALLBOX_IDLE_POLL_INTERVAL_S` (20 minutes). For the single-phase
  case above, where the load estimate is blind, this interval is the only
  brake there is.

`_charger_drawing` deliberately ignores the charger's own `charging` flag:
this box reports status 3 ("Charging") throughout a pause *and* for a
plugged-in car that has finished, with 0 A on every phase. Only the power
and the phase currents tell a live charge from a finished one.

Known limitation: a single-phase charge is still interrupted once per
`WALLBOX_IDLE_POLL_INTERVAL_S`, costing roughly 10% of its throughput
against 39% before. Closing that gap entirely would mean lowering
`WALLBOX_BUSY_LOAD_KW` into the range of ordinary household appliances,
which would suspend polling for an oven.

**Both strategies are kept, and `WALLBOX_POLL_MODE` picks one.** `gated` is
the above. `direct` reads the charger every `WALLBOX_POLL_INTERVAL_S`
whatever the house is doing - the behaviour from before the fault was found.
The switch exists because the fault is the charger's own, which means a
firmware change can end it, and the only way to find out is to read again
and watch whether a charge survives. So `direct` is a test rather than a
setting to leave on, and the default - along with anything unrecognised - is
`gated`. The active mode is reported in `/api/live` as `wallbox.poll_mode`
and named on the dashboard panel, because the difference is whether a charge
can be cut short.

Energy accounting does not change with the mode. It comes from the charger's
own counter either way (section 9), never from integrating the reported
power; `direct` only means the counter is observed every five minutes
instead of once after the charge, which shortens the spans the backward walk
has to place.

**The re-test, 28 Oct 2026: the fault is unchanged.** What prompted it was a
different behaviour disappearing on the same day. Until then, a car charging
with the house battery low pulled the battery along: on 24 Oct at 18:37 the
house drew 10.15 kW with no sun and 10% state of charge, and the inverter
charged the battery at 6.07 kW on top, for 15.96 kW across a 25 A
connection - 23 A per phase, 93% of the fuse. On 71% of that day's
high-load samples the battery was charging at a mean of 2.76 kW. On 28 Oct
at 20:54, same 10% charge and no sun, the house drew 11.44 kW and the
battery stayed at 0.00 kW: 11.60 kW from the grid. Same conditions,
opposite dispatch, nothing here changed it - which is the nearest thing to
evidence that something in the charger or its firmware had moved, and the
reason to read it again.

It had not moved. Three interruptions in eleven minutes, each on the poll
interval:

    22:36:10-22:37:52  charging   16.1 / 15.7 / 22.0 A   12.6 kW
    22:38:12           counter 90.6 -> 91.0, so a read   current collapses
    22:38:12-22:40:52  pause      2.5 / 1.6 / 7.6 A      2:40
    22:41:12-22:43:33  charging   16.2 / 15.8 / 21.7 A   2:21
    22:43:53           counter 91.0 -> 91.4, so a read   current collapses
    22:43:53-22:46:55  pause                             3:02
    22:47:15-22:48:35  charging   15.2 / 14.9 / 19.1 A   1:20
    22:48:55           third read due                    current collapses

Interruptions 5:41 and 5:02 apart against a 300 s interval, and two of the
three coincide with an *observed counter change* - direct evidence that a
read happened at that moment, rather than an inference from timing as in the
original measurement. Duty cycle 41%, so worse than the 60% measured on 25
Oct. The charger reported status 3 and 10.1 kW at the first read while the
independent meter already showed the collapse.

A by-product worth keeping: with the car at about 16 A per phase, L3 reached
22.0 A of 25 A, because the standing base load of section 8 sits on that
phase nearly alone. The highest current this installation has been measured
at is not a charging car - it is a charging car plus a battery being filled
from the grid at the same time, at 23 A.

Two lessons worth keeping:

- **The risk was already written down.** The docstring of
  `GoodWeWallboxReader` cited #30669 and named the exact test - "a charge
  that stops within a poll interval of a reconnect". An earlier measurement
  had dismissed it, but that measurement was taken with no car charging,
  which is the only condition under which the fault can appear.
- **A constant period rules out a power-dependent cause.** That one fact was
  available from the start and would have skipped every wrong hypothesis.

### 8. The inverter's own numbers do not add up

Everything the inverter reports about itself is in one of four figures: PV,
house load, battery and grid. They should balance, and they don't. Energy in
minus energy out leaves a remainder, always positive, in every operating
state:

| State | Missing | Samples |
|---|---|---|
| Battery idle, no PV (no conversion anywhere) | 0.12 kW | 6 189 |
| PV running, battery idle | 0.09 kW | 7 616 |
| Battery charging | 0.11 kW | 12 442 |
| Battery discharging | 0.18 kW | 33 794 |

The decisive property is that the remainder is **constant in absolute terms,
not proportional**. Across the discharge range it stays between 0.15 and
0.21 kW while the share falls from 26% at 0.5 kW to 1.6% at 13 kW. A
conversion loss would scale with the power; a fixed draw does not. And it is
present at 0.12 kW even with the battery idle and no PV, where the inverter
is doing nothing but passing grid power through to the house and no
conversion happens at all.

That points at the inverter's own auxiliary supply: it sits between the
meter point and whatever the inverter counts as "Total Load power", so it is
paid for and never reported. At 0.12-0.19 kW continuously that is 3-4.5 kWh
a day, or 1 100-1 600 kWh a year.

What the inverter cannot do is witness this about itself - every number in
the balance comes from the same device. A Shelly 3EM on the three main
phases settles it from outside. Fourteen simultaneous samples agreed with
register 35139 to 5 W on average (spread -20 to +27 W) while the connection
point sat near zero, which rules out a miscalibrated grid measurement and
leaves the gap inside the inverter: the battery delivers 0.86 kW, the load
register reports 0.69 kW, the second meter confirms that nothing leaves to
the grid.

Two things this does not yet establish. The agreement was measured at a
connection point near zero, so it pins the zero point and not the scale -
the comparison at a real flow of several kW comes from the logged history.
And a constant offset in the load register itself would look identical from
outside; separating that from a real draw needs the utility meter, where
3-4 kWh a day more import than the dashboard accounts for would confirm the
draw is real.

Sign conventions differ between the two meters, which is worth stating once:
the Shelly reports import as positive power and accumulates export in a
separate counter, the inverter's register 35139 is negative while importing.
`ShellyClient` flips the Shelly's sign on the way in, so everything stored
and served is export-positive. The flip was not guessed: watching which
counter moved while two phases read -160/-210 W and one read +380 W settles
it in 75 seconds.

A side finding with its own consequence: the phases are not balanced and the
current is not proportional to the active power. At a connection point
summing to -15 W the individual phases still carried 1.3, 1.3 and 2.0 A at
power factors between -0.45 and 0.78. The 25 A main fuse is per phase and
carries RMS current, so fuse headroom cannot be estimated from active power
- which is all the inverter offers. The Shelly measures it directly.

### 9. The fix for section 7 broke the energy page

Two days after the poll gate went in, charging stopped appearing on the
energy page altogether. The car had taken 12.9 kWh across those two days and
the page showed none of it - it was counted as general household load
instead.

The cause was the gate working as designed. The page integrated the
charger's recorded power, and the whole point of the gate is that the
charger is not read while a car is drawing, so that power now reads 0 for
the entire duration of a charge. The fix in section 7 and the thing it broke
are the same mechanism; nothing was wrong with either on its own.

Integrating that power had been wrong before the gate too, in the opposite
direction. The charger is polled every five minutes and the snapshot is
repeated into every 70-second row, so each real sample was counted four or
five times: across ten days that produced 221 kWh against the charger's own
counter reading of 88 kWh. The page had been overstating EV charging by a
factor of 2.5 and nobody noticed, because an inflated number looks like a
number.

**What replaced it.** Register 10065 is cumulative and exact - 78.1 kWh
before a charge, 88.0 kWh after, with no read in between - so the counter
sets the scale. It cannot say *when* the energy flowed, because it is only
ever observed after the fact. That comes from the house load above the
heater.

Which samples may have hidden a charge is deliberately not a new threshold.
It is `WALLBOX_BUSY_LOAD_KW`, the control loop's own definition of "a car is
drawing" - the number that decides whether the charger may be read at all.
The two uses are the same statement: where it holds, the charger was not
read and the counter stood still, so the energy has to be placed by hand;
where it does not hold, the charger was read and the counter moved on its
own.

**Where exactly comes from the gate's timing, not from weighting.** The
first attempt weighted every sample over the threshold anywhere in the span,
and a span can be 21 hours long - so a 3.5 kW household load three hours
before the car was plugged in collected 0.53 kWh of charging.

But the gate only reads the charger again once the house has been quiet for
`WALLBOX_IDLE_TICKS`, which means the read that observes an increment
follows the last charging sample by that many ticks and no more. Walking
backwards from the observation and stopping at the first longer quiet run
therefore finds the charge and nothing before it. On both charges of 26 and
27 Oct 2026 the quiet tail was five samples long on the nose - the gate's
own signature in the record.

The walk carries on past a long quiet run while the samples found so far
still cannot carry the increment, because then the block is incomplete by
definition. That is what charging before the gate existed looks like: a
burst every few minutes with pauses the counter was never read in.

**What it is worth.** On the charge with both kinds of evidence, all of the
9.9 kWh landed in the two hours it ran, and the two days hold two sessions
where there were two charges. Daily and monthly totals are exact, because
they are the counter.

Three things it cannot do. An appliance over the threshold *during* the
charge still takes a share it did not earn - nothing here can tell a 3 kW
oven from 3 kW of car. Charging from before the counter worked (it read a
flat 0.0 until 23 Oct 2026) is absent rather than estimated. And a charge
shorter than the gap between two reads is placed at the read that saw it
with no power attached, because dividing a whole charge by one 70-second
sample would claim several hundred kW - which is also why derived power is
capped at the house load above its own base: where the service was down for
part of a charge, the surviving samples have to carry the whole increment,
and the quotient claimed 102 kW from an 11 kW box.

Sessions come from the same series now. They had been built from the
charger's status flag, which reads "Charging" for a finished car sitting at
0 A on every phase - so the one-hour charge appeared as a 63-minute session
starting two minutes after it ended, carrying 0 kWh.

Two lessons worth keeping:

- **A fix that removes a measurement breaks everything downstream of it.**
  The gate was verified against the thing it was built for, that the car
  charges without interruption, and it does. Nothing checked what else had
  been reading those samples.
- **Prefer a counter to an integral wherever one exists.** The integral was
  wrong by 2.5x for days before it was wrong by 100%, and only the second
  kind of wrong was visible.
- **A reconstruction has to be checked against something that knows.** Every
  version of this balanced to the counter and looked finished. What found the
  errors was being told that 12:37 was not a car, and that an 11 kW box
  cannot deliver 102 kW.

## Setup

```
pip install -r requirements.txt
cp .env.example .env   # fill in the addresses of your inverter and AskoHeat
uvicorn app.main:app --reload
```

Then open `http://127.0.0.1:8000`. Note that this talks to the real devices
as soon as it starts, heater writes included - see the warning under
[Deployment](#deployment) about running two instances at once.

## Deployment

In day-to-day use this runs as a long-running service on a small always-on
machine (a Raspberry Pi is plenty). It has to sit on the same network as the
inverter and the AskoHeat, since both are reached over local Modbus TCP - it
needs no access to the internet at all.

Unlike the development command above, drop `--reload` and bind to the
network so the dashboard is reachable from other devices:

```
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Keep it running across reboots with a service manager. A minimal systemd
unit, with the placeholders filled in for the target machine:

```ini
[Unit]
Description=AskoHeat Manager
After=network-online.target

[Service]
WorkingDirectory=<path to this checkout>
ExecStart=<path to the virtualenv>/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
User=<service account>

[Install]
WantedBy=multi-user.target
```

**Run only one instance at a time.** Every instance drives the same heater,
so a second one started for testing will fight the first over the heater
step, each undoing the other's writes every poll. Stop the service before
running the app by hand against the real devices.

### Updating

```
git pull
sudo systemctl restart <service name>
```

Three things worth knowing about an update:

- **The database migrates itself** on startup. Columns are added additively
  and rows recorded before they existed keep `NULL`, so there is nothing to
  migrate by hand and no reason to delete the history.
- **`.env` never travels with a pull** - it is git-ignored and stays on the
  machine. A new setting therefore has to be added there, or left to its
  default in `app/config.py`, where every setting has one.
- **Hard-reload the dashboard** (`Ctrl+Shift+R`) after any change under
  `app/static/`. Otherwise the browser keeps serving the cached `app.js` and
  the page looks unchanged even though the deployment succeeded.

To confirm the controller came back up healthy, check that the poll log line
shows real values rather than `?` placeholders:

```
step=3 pv=7.08kW consumption=0.93kW battery_charge=6.07kW battery_discharge=0.00kW soc=62% grid=0.03kW -> askoheat_load=1900W temp=52.3C [battery] ...
```

A `soc=?%` there means the SOC register could not be read. The controller
then deliberately treats the battery as not full and leaves the heater off,
so this fails towards no heating rather than towards draining the battery -
see [Control logic](#control-logic).

## Losses on the energy page

The energy page reports three loss buckets rather than one efficiency
figure, because no two of them are measured the same way and only two of
them are losses at all.

**Inverter standby** is what went into the inverter and never came out of it:
`pv + battery discharge + grid import - house load - battery charge - grid
export`, summed per sample. It is worth keeping separate because it does not
behave like a conversion loss - it measures ~0.1kW whether the roof is making
1kW or 10kW, so it is the inverter's own draw, not a percentage of throughput.
Over six weeks it was the largest bucket of the three.

**Battery round trip** is `charged - drawn - still stored`, where the stored
part is the SOC change times `BATTERY_CAPACITY_KWH`. Without that correction
the figure is meaningless over short windows: a morning that put 18kWh in and
took 4kWh out is a full battery, not a 78% loss. The page therefore suppresses
the number entirely when more than a quarter of what went in is still in
there, and says so instead of printing it. Measured over the full record:
87% round trip.

Only rows that carry an SOC are counted, so that the throughput and the SOC
change describe the same span - the rows recorded before the SOC column
existed otherwise contribute discharge with no charge to match, which puts
the round trip at 93% for a battery that measures 87%.

**Held back** is production the plant throttled away, and it is a lower bound,
not a measurement: nothing here records what the roof could have produced. In
a 15-minute window during which the battery was full and nothing went to the
grid, the plant has demonstrated with its own best sample what it was capable
of, and every sample below that was the inverter throttling production down to
the house load. Anything it held back for a whole window is invisible to this.
The window length is the whole argument - the same data gives 64kWh at 15
minutes, 97 at 30, 407 at 60 and 883 unbounded - so the page uses the shortest
window that still contains a usable reference and marks the result with a
"greater than". It is listed next to the other two but is deliberately not part
of the headline figure: it is energy that never existed, so adding it to energy
that was measured going in would be double counting.

The page also splits the held-back energy by who could have absorbed it. That
split is the only number on the page that grades the control loop rather than
the hardware, and it is the reason the loop is worth having: over the recorded
period 54.6kWh was held back with the tank already too hot to start a heating
cycle, 4.9kWh with the heater at its last step, 4.3kWh while the heater still
had steps left - and 0.1kWh with a cold tank and the heater off, which is the
only case the loop could have prevented and did not.

## Known limitations

- Local Modbus/TCP is not enabled on a GoodWe data logger by default and
  cannot be switched on from the outside - GoodWe support has to enable it
  per device (see problem #2). Without that, this approach does not work at
  all, and the cloud API is not a usable substitute.
- The heater is regulated one step per poll, so it takes a few minutes to
  work its way up to the right step after a change in conditions. That is
  deliberate - the steps switch mechanical relays in the AskoHeat.
- Register addresses and the value-derivation formulas above are specific to
  this particular installation (GoodWe GW15K-ETA-G20 hybrid inverter +
  AskoHeat+) and firmware version, and may not generalize to other models.
- `.env` holds the addresses of your own devices and is git-ignored - never
  commit it.

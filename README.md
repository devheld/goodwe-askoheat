# Home Energy Manager

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
- `app/shelly_client.py` - `ShellyClient`: reads a Shelly 3EM over HTTP, one
  instance per clamp meter. At the grid connection point it is a second,
  independent meter and the only instrument here that measures the RMS
  current through the main fuse; on the heat pump's feed it measures a load
  nothing else here can see; on the charger's feed it measures a load that
  could not be measured even in principle, because reading the charger stops
  it (sections 7 and 10)
- `app/database.py` - `HistoryStore`: SQLite persistence for the dashboard's
  history charts
- `app/main.py` - FastAPI app serving `/api/live`, `/api/history`, and the
  four static pages in `app/static/`

The pages are `/` (the house: power, roof sides, compressor runs, battery),
`/asko`, `/wallbox`
and `/energy`. The first three are one script, `app/static/app.js`: they read
the same two endpoints and what a page draws is decided by which containers
its HTML has, so every renderer tolerates its own absence rather than
throwing on a missing element. `base.css` holds the tokens and the shared
components; `nav.js` and `nav.css` are the navigation, which carries one
accent colour per destination and shows only one of them at a time - the
page you are on, or the one under the pointer. Four coloured icons side by
side would be a categorical palette nobody can read (blue against violet is
10.5 delta-E for a normal-vision reader, 4.5 for a deutan).

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
drawing. Between skipped polls the last snapshot is reused and the dashboard
says why it is standing still.

What counts as drawing has since changed instrument - see section 10. It was
the household load above the heater, compared against `WALLBOX_BUSY_LOAD_KW`
(3.0 kW by default - above a house base load, below a three-phase car at the
6 A minimum of 4.14 kW), which remains the fallback. It is now a clamp meter
on the charger's own feed and `WALLBOX_METER_BUSY_KW` (0.1 kW), which
measures the car directly. The three paragraphs below describe the inferred
gate, and are what the clamp made unnecessary rather than what runs.

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

That left one limitation, and it was the whole reason for the clamp meter: a
single-phase charge was still interrupted once per
`WALLBOX_IDLE_POLL_INTERVAL_S`, costing roughly 10% of its throughput
against 39% before. Closing the gap with the household load was impossible -
it would have meant lowering `WALLBOX_BUSY_LOAD_KW` into the range of
ordinary household appliances, suspending polling for an oven.

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

**Three meters instead of one.** A clamp meter is the only instrument here
that can be put where a question is, and the connection point is not the
only question in the house. `SHELLY_GRID_HOST`, `SHELLY_HEATPUMP_HOST` and
`SHELLY_WALLBOX_HOST` each take one device, and the name a device is
configured under decides two things: the sign it is read with, and the
columns it is written to - `shelly_*`, `heatpump_*`, `wallbox_meter_*`. The
three series therefore cannot be mistaken for one another, and a meter that
is absent or away leaves NULL rather than a zero that would read as
"measured nothing". Only the grid role is a flow with two directions; on a
load the device's own sign is kept, so consumption is positive and a clamp
mounted backwards reads negative, which is the one mistake a clamp can make
silently.

The one on the charger's feed is not there out of curiosity. It measures
from outside what section 7 cannot ask for from inside: charging power per
phase, on every tick, while the charger itself is left alone. It turns the
poll gate from an inference about household load into a measurement, and it
replaces the backward walk of section 9 with a counter. `wallbox_meter_kw`
and `wallbox_power_kw` are deliberately separate columns: one is what the
charger says about itself, the other is what it actually drew, and the whole
of section 7 is the gap between the two.

Per-phase house load no longer depends on any of them. The inverter has it
in the block it is already read from: `35163/35165/35167` carry the load per
phase and sum to `35171` within a watt (2885 against 2886 when this was
checked), and they are recorded as `load_l1_kw`/`load_l2_kw`/`load_l3_kw` on
every row. What a clamp still has that the inverter does not is current:
active power per phase is not fuse headroom, for the reason just given.

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

### 10. A clamp meter on the charger's feed, and what it made obsolete

Sections 7 and 9 are both consequences of one fact: the charger cannot be
asked how much it is delivering while it is delivering. Everything they
describe - the gate inferred from the household load, the counter
increments spread backwards over the samples that probably held the charge,
the threshold that could not see a single-phase car - is work done to avoid
needing a measurement that did not exist.

On 31 Oct 2026 a third Shelly 3EM went onto the charger's feed, and it does.

**The gate.** `WALLBOX_METER_BUSY_KW` replaces `WALLBOX_BUSY_LOAD_KW`
wherever the meter answers. The numbers say why that is not a small change:
the charger's standby is 4-7 W, and the smallest charge it can deliver is a
single-phase car at the 6 A minimum, 1.38 kW. A threshold of 0.1 kW sits
twenty times above the one and fourteen times below the other. The household
estimate had to sit at 3.0 kW to keep an oven out, so it could not see that
charge at all - and it counted the oven as a car. The clamp measures the car
and nothing else, on the same tick as the decision. The hysteresis
(`WALLBOX_IDLE_TICKS`) stays: the clamp sees a pause clearly, but what a
pause means to the car is unchanged.

**The energy.** `wallbox_meter_kw` is integrated like any other power
column. No counter, no weighting, no `_charge_block` walking backwards from
an observation. The three limitations at the end of section 9 - an oven
inside a charge taking a share, a charge shorter than the gap between two
reads landing with no power attached, derived power capped at the house load
- do not apply to a sample that was measured. The machinery stays in
`_wallbox_series` because the history it explains is still in the database,
and every sample carries `metered` so a measured session and a reconstructed
one are never presented as the same kind of fact. The session list says
which each one is.

**Does the charger's Modbus still earn its slot?** It no longer carries a
single number the dashboard depends on. What it still answers, and the clamp
cannot:

- whether anything is plugged in at all, and which RFID card it was
- the charge mode (fast / PV surplus / PV + battery) and the configured
  power limit - the settings, as opposed to their effect
- its own lifetime counter, register 10065, as a second witness to the
  energy
- the hardware's own description of itself: 22 kW, three-phase, phase
  switching enabled

All of that is description rather than measurement, and description does not
change while a car charges - which is precisely when the box must not be
read. The two constraints no longer collide, so the reader stays, demoted:
read on its slow clock, gated by the clamp, and labelled on the page as the
charger's own account rather than as the measurement.

**Do the three witnesses agree?** `/api/wallbox/crosscheck` compares them
over the record and the wallbox page shows the result. As of the first 74
minutes: the clamp reads 5 W of standby where the charger reports 0.0 kW,
which agrees as far as a register quantised to 0.1 kW can agree, and the two
counters differ by 8 Wh, which is below the charger's own 0.1 kWh
resolution. Both are consistent and neither is informative. The comparison
that will settle it needs a charge, and it is the counters: both are
cumulative, so differencing them over a window is independent of how often
either was read.

### 11. The charts were unreadable on a phone

Every chart was drawn in a fixed 960-unit coordinate system and left to the
SVG `viewBox` to fit whatever container it landed in. On a desktop that is
roughly 1:1 and nobody notices. Inside the ~310 px content column of a
phone it is a factor of 0.32, and a `viewBox` scales *everything* in it -
an 11 px axis label came out under 4 px. The same scaling squashed each
chart to a third of its height, because `height: auto` keeps the viewBox's
aspect ratio.

The coordinate system is now the container's own measured width
(`chartWidth`), so one unit is one pixel: type is the size it says it is
and a 220 px chart is 220 px tall on a phone as well. Width is read at draw
time, so every page redraws on resize - which is what a rotating phone is.

What had been tuned for 960 px and had to become responsive: the x axis
drops to three or four labels below 700 px; the energy page thins its
bucket labels by the room the band actually has; the donut gives up radius
and its labels give up their names, which the legend carries; the session
chart shortens its date gutter and drops the cost from the bar label. Data
tables scroll inside their own box below 560 px - a six-column table cannot
shrink to 310 px, and one that is wider than the page makes every chart on
it scroll sideways too.

One thing the pixel sizing exposed rather than caused: a chart whose values
all round to the same label. The clamp on the charger's feed reads 5 W of
standby, so all five y ticks said "0.0" and the repeat-dedupe left one
label above four blank gridlines. The axis now takes as many decimals as it
needs to separate its own ticks.

### 12. The heat pump heats at night, and nothing can be asked why

The heat pump is a CTA Optiheat 1-5es: a Siemens RVS61 controller behind an
OCI670 gateway. Section 10's conclusion about the charger does not transfer
here, because this machine has no readable interface at all. Port 502 is
closed, the gateway's own web endpoints answer `auth failed`, and the
OCI670 cannot speak Modbus in any configuration - the retrofit for that is
a separate module (OCI 351.01/109) plus an Optiplus controller of a recent
enough series. So no setpoint, no operating mode, no flow temperature can
be read, and nothing can be written. The clamp meter on its feed is the
only witness to what the controller decided.

It turns out to be enough, because the three things this machine can be
doing are far apart in watts:

| State | Measured |
|---|---|
| Controller alone | 0.035 kW, around the clock |
| A circulation pump turning | 0.13-0.14 kW, L3 only |
| Compressor running | 1.10-1.21 kW, unbalanced across the phases |

A threshold at 0.5 kW therefore separates "heating" from "idling" with two
orders of magnitude of room on either side, and that is all a run detector
needs. `heatpump_runs` walks the recorded samples, groups the ones above
the threshold into runs, and measures each one: start, end, duration,
energy, mean and peak power. A break longer than ten minutes starts a new
run, so a service restart in the middle of one does not produce a single
run that covers the outage. Energy is power times interval like every other
figure here rather than the clamp's own counter, which restarts with the
device and would read a straddling run as the meter's entire lifetime.

The useful comparison is against the hours the pump is *meant* to run in
(`HEATPUMP_WINDOW_FROM_H`, `HEATPUMP_WINDOW_TO_H`). That window is not a
control and cannot become one - nothing here can switch this machine - it
is only the line the record is measured against. Energy outside it is
classified per sample rather than per run, so a run from 07:30 to 08:30 is
half a breach instead of being rounded to a whole answer either way.

It is two windows, not one, because the controller holds two time
programs: a later start on working days than at the weekend.
`HEATPUMP_WEEKEND_WINDOW_FROM_H` and `..._TO_H` carry the Saturday and
Sunday pair and fall back to the weekday pair when they are unset, so a
house on the same hours all week still configures one window. A single
pair could only be set to the earlier of two starts, which would read
every working morning before it as a breach, or to the later one, which
would excuse every weekend morning - and a measurement that is wrong two
days out of seven is not a measurement. The classification is per sample
and per local weekday, which also makes the night between two days
asymmetric: Friday 18:00 to Saturday 08:00 is fourteen hours, Sunday 18:00
to Monday 10:00 is sixteen, and the breakfast line spans whichever night
it actually was.

The shaded band on the chart is therefore drawn per row instead of once
behind all of them. Each band is the full row height, so days on the same
program touch and a working week still reads as one block with the weekend
stepping out of it, rather than as seven separate marks.

**The chart is a clock, not a bar chart**, because the question is *when*
and not how much: 00:00 to 24:00 across, one row per day down, the intended
window shaded behind the rows. A run outside the window then needs no
colour of its own to read as wrong - it is a bar sitting outside the band -
and days stacked under each other turn a change made at the controller into
a before-and-after that can be seen rather than remembered. Lowering a
reduced setpoint is one line of evidence per night after that, instead of
an impression.

Two blanks mean opposite things on a chart like this, so they cannot look
alike: a day with no run is the result a time program is supposed to
produce, while a day before the clamp was fitted is nobody having looked.
The recorded span is hatched where it ends, rows begin at the first day
there is a record for, and a recorded day with no run says "none" where an
unrecorded one says nothing.

What the measurement showed on the first night: five runs between 18:39 and
07:27, each 55-60 minutes at a flat 1.15 kW, with the pauses between them
shrinking from 2 h 37 to 1 h 35 as the night got colder. A hot-water
recharge has no reason to tighten its cadence as the outside temperature
falls; a weather-compensated heating circuit does exactly that. The shape
of the record identified which of two settings was responsible, which is
the kind of question that otherwise needs a controller one can read.

### 13. kWh are not a unit anyone has a feel for yet

A house that was heated with oil until recently has years of intuition in
litres and none in kilowatt-hours. "The pump used 10.3 kWh yesterday" is
unreadable in both directions: nobody knows whether that is a lot, and
nobody knows how it compares with the tank that used to be refilled every
spring. So the pump's energy is also shown in litres of heating oil.

The conversion is three assumptions and one measurement, and the dashboard
prints all four rather than just their product:

| | Value | Where it comes from |
|---|---|---|
| Electricity in | measured | the clamp meter on the pump's feed |
| Heat out per kWh | `HEATPUMP_SPF`, 4.0 | assumed - nothing here measures heat |
| Energy in a litre of oil | `OIL_KWH_PER_LITRE`, 10 | lower heating value, 35.9 MJ/l |
| What a boiler did with it | `OIL_BOILER_EFFICIENCY`, 0.85 | annual utilisation of an old non-condensing boiler |

Only the first line is a measurement, and the second is the one that
matters most, so it is worth saying where 4.0 comes from. The machine's
datasheet gives COP 4.5 at B0/W35, 3.4 at B0/W45 and 2.7 at B0/W55, drawing
1.1, 1.4 and 1.7 kW. The clamp reads 1.10-1.21 kW, which is the B0/W35
column almost exactly - so the machine is currently working at the top of
its own range, and the instantaneous figure would be around 4.5. Across a
winter it will not be: the brine gets colder and hot water needs a higher
flow temperature. 4.0 is a seasonal estimate below the datasheet's best
point, and the boiler is given the optimistic end of its range, both
erring in favour of the heating that is being replaced.

The result appears twice, in the two places the question gets asked. On the
energy page it is a tile beside the pump's own, period-scoped like
everything else there, carrying the pump's colour because it is the same
energy and not a second thing. On the front page it is appended to the
nightly verdict, where the point is not the total but the comparison: a
night of unwanted heating reads differently once it is a number of litres.

### 14. Has it paid for itself yet

The `Finanzen` card on the front page is two bars on one scale: what the
installation cost after everything that came back, and what it has saved
since the recording started. The distance between the ends of those two
bars is the whole answer, which is why it is drawn rather than reduced to a
percentage, and the day the lower bar passes the upper one is the day the
thing is paid off. After that the same counter keeps going as profit.

The cost side is four figures and one piece of arithmetic that is easy to
get wrong:

| | |
|---|---|
| `INVESTMENT_CHF` | the invoice total, gross |
| `SUBSIDY_CHF` | the grant actually paid out |
| `TAX_DEDUCTION_CHF` | the amount that may be deducted |
| `TAX_DEDUCTION_RATE` | the share of that which became money |

A deduction is not a payment. Subtracting the deductible amount in full
treats a franc of deduction as a franc in hand, and it is worth the
marginal tax rate - a quarter to a third of that in most Swiss communes.
The rate therefore defaults to 1.0, which takes the figure at face value as
entered, and lowering it to the real marginal rate is the single change
that moves the break-even date most. **None of these amounts are in this
repository.** They live in `.env`, which is not in git; `.env.example`
carries zeros and this explanation.

The return side is measured savings only, in three streams, each divided by
the hours it was actually recorded over rather than by the calendar:

- **Solar used in the house** - the purchase it replaced, at the import
  tariff.
- **Solar exported** - at the (much lower) export tariff.
- **Heating instead of oil** - the oil the heat pump did not burn, minus
  the electricity it did use. The electricity is subtracted here and
  nowhere else: the solar stream already prices the kWh the roof supplied
  whichever load took them, so crediting the pump with free power as well
  would count the same francs twice.

Per-stream day counts matter more than they look. The clamp on the heat
pump went in eleven weeks after the record starts, so its savings cover a
few days while the solar streams cover months. Dividing everything by one
span would report a daily rate the machine never ran at. `FINANCE_RETURN_STREAMS`
decides which streams count at all, because whether heating belongs in this
sum is a fact about the invoice: if the heat pump was bought separately, its
savings are paying off something else.

What the card does **not** do is annualise. Eleven weeks of late-summer sun
is not a year. So the break-even line prints the rate and the number of
recorded days behind it, and says in as many words that a winter will move
it. A projection that hides its own basis is a promise rather than an
estimate.

### 15. The benchmark: is it doing what it was sold as doing

A counter answers "how much so far". It does not answer the more useful
question, which is whether the installation is performing as quoted - and
that question has an authoritative answer sitting in a drawer, because the
quote that led to the order contains its own economic model. Putting that
model into `BENCHMARK_*` turns the card from a counter into a comparison: a
third bar, dashed because it is a target and not money in hand, showing
what the quote said would be in the account by now.

Three decisions make the comparison fair rather than flattering:

**It is compared over recorded days, not elapsed days.** Those differ by a
week here, and the quote has no opinion about a service outage. A rate per
recorded day measured against a promise per elapsed day would hand the
installation a free week.

**The quote's tariffs are kept separate from `PRICE_*`.** A promise is
compared at the prices it was made at, or the comparison measures the
tariff rather than the roof. The two sets differ here by about 15%, which
is larger than most of the effects anyone would want to read off this card.

**The quote's annual figures are spread over the window's own months, not
divided by 365.** The quote's model is a flat annual return from year one,
and for a while it was tempting to use it as the straight line it is - its
seasonality is not in the document, and inventing one felt like forging it.
That reasoning does not survive a short window. A comparison that covers
November only would be measured against a twelfth of a year's sun when
November carries about a twenty-eighth of it, and the ratio would read as a
fault in the roof rather than as winter. So `BENCHMARK_MONTHLY_SHARES`
distributes the quoted annual kWh across the months before any part of it
is compared with anything. It is the one assumption in the benchmark that
does not come from the quote, it is stated on the card, and it matters
less the longer the window gets: over a full recorded year the shares sum
to one and the profile drops out of the arithmetic entirely.

The card reports two ratios and not one, because they measure different
things and only mean something together: francs saved against francs
promised, and kWh produced against kWh promised. They can come apart badly.
On this installation a battery that was never in the quote keeps nearly
every kWh in the house at the import tariff, instead of exporting half of
it at a third of the price - so the money holds up considerably better than
the production does, and a single blended number would have hidden both
facts.

### 16. One price is not a price

Electricity tariffs change on a calendar, usually at new year. A counter
that runs for a decade and prices its whole history at today's rate is
wrong in a particular way: every time the utility publishes a new sheet,
savings that were already earned move. So `TARIFFS` is a history rather
than a number - a year, an import price, an export price - and energy is
priced bucket by bucket at the rate that applied then. The key is a year
and not a date because the utility publishes once a year, and a date field
would only invite guessing at a day nobody knows.
`PRICE_IMPORT_CHF_PER_KWH` and `PRICE_EXPORT_CHF_PER_KWH` remain as the
fallback for years before the first entry.

Two consequences show up on the pages. A period that crosses a change has
no single price, so where one is printed it is the hours-weighted average
and is marked with a diameter sign; the figures above it were each priced
at their own rate, and a bare number would claim otherwise. And the loss
figures are priced at that average, because they are totals over the whole
period and are not broken down per bucket - the one place here where the
tariff is applied to a sum rather than to a span.

This is also why the quote's tariffs live in `BENCHMARK_*` and not in
`TARIFFS`. They are not a price this household ever paid; they are an
assumption inside someone else's model, and the benchmark needs them to
reproduce the figure that model printed.

### 17. Under an export ban, output is a permit and not a measurement

Feed-in needs the utility's approval, and it arrives after the roof does.
Until it comes the inverter is held to the house's own consumption: the
moment production would pass the load and a full battery, it is throttled.

The first instinct is to treat that as a known loss and correct for it -
publish the curtailed kWh, subtract the export revenue the quote expected,
compare what is left. That instinct is wrong, and the reason is worth
writing down. The curtailment this project can measure is the surplus the
**AskoHeat** could not place; it cannot see the production the inverter
never made, because nothing records a kilowatt-hour that was declined. The
roof has never once been allowed to show its maximum. A ratio of recorded
production against a predicted annual yield is therefore not merely
imprecise, it is uninterpretable: the size of the error is unknown and so
is its sign.

So the benchmark does not correct those months, it excludes them.
`EXPORT_ALLOWED_FROM` opens the comparison window and nothing before it
counts toward either ratio. Two things follow:

- **The savings from the excluded months stay in the counter.** They are
  measured francs that were really not spent, and the payback is a cash
  question. Only the comparison needs a clean window; the money does not.
- **A window needs a length before it means anything.**
  `BENCHMARK_MIN_DAYS` holds the ratios back until there are enough days,
  and until then the card says how many of them it has. A percentage
  computed from four days of November is noise with a decimal point, and a
  card that prints one teaches its reader to stop believing it.

The honest statement about the excluded period is that it cannot be
judged, and that is what the card says. A back-estimate becomes possible
later - once some unthrottled months exist, they calibrate what the
throttled ones could have produced - but it would be an estimate built on
the window, so the window has to come first.

### 18. Costs that are not on the invoice are still costs

The quote priced a roof. What was built is a roof, a battery, a heat
pump's clamp, an immersion heater and a wallbox, and the wallbox was paid
for outside the solar contract. `INVESTMENT_EXTRA_CHF` exists so that it
lands on the same page as its own effect, because the effect is large and
in the flattering direction: a car is the biggest single reason
self-consumption runs above the quoted plan, and the quote's closing
paragraph says as much in its own words. Counting the charging in the
savings while leaving the charger out of the cost would be a payback
calculation with its thumb on the scale.

### 19. The redundant clamp was measuring the wrong thing

The clamp meter at the house connection duplicates the inverter's own grid
reading. Over 7234 paired samples the two differ by a median of 16 W and a
95th percentile of 97 W; the 1.3% that differ by more than half a kilowatt
are sampling skew, two instruments read a second apart with a compressor
starting in between, not disagreement. As a cross-check it has done its
job and has little left to prove.

But a 3EM does not only report a total. It reports current per phase, and
that the inverter cannot: its registers 35163/35165/35167 give load per
phase, which is not what the connection carries. Those three columns were
being written to the database every minute and read by nothing - and they
already held a 22.0 A peak on one phase of a 25 A fuse, at 22:37 on a
Wednesday evening, which no page would have shown anybody.

So the fix was not to move the clamps. It was to use the quantity only they
can see:

- **The axis is the fuse, never the data.** A bar chart that rescales to
  its largest value would show a comfortable evening and a near-trip as the
  same picture. The track is the full rating, and a reading above it is
  clamped with the real number still printed beside it.
- **The peak is the headline, not the live value.** A fuse is not tripped by
  an average, and nobody is at the dashboard when it would matter. Each
  phase carries its window peak as a marker, and the sentence at the top
  leads with the worst ever recorded and when it happened.
- **The threshold is a line, not a colour.** `MAIN_FUSE_WARN_PCT` is drawn
  on the track, so "too close" is a position - the same device as the
  heating window on the compressor chart, and it survives colour-blindness
  and a monochrome print alike.

`MAIN_FUSE_A` is a setting and not a reading, and the card says so. Nothing
here protects anything; the fuse does that. This only makes the distance to
it visible before another load is added, which is exactly the question a
house with a car, a heat pump and an immersion heater on a 25 A connection
keeps being asked.

### 20. The car is worth more than the tariff, and the split does not matter

A kWh into the car at home is not worth the 0.25 it saved on the
electricity bill. It is worth what the same kWh costs at a public charger,
which here is at least 0.85 - the alternative to charging at home is not
charging for free, it is charging somewhere else.

The obvious way to count that is to split the charging: kWh that came off
the roof saved the full 0.85, kWh that came off the grid saved 0.85 less
the 0.25 they cost. That is where the arithmetic gets interesting, because
the two cases are the same number.

The counter already credits every self-consumed kWh in the solar stream at
the import tariff, whichever load took it. So a solar kWh in the car has
already been paid 0.25 and may only be topped up by the difference; a grid
kWh in the car has been paid nothing and is worth 0.85 less the 0.25 it
cost. Both come to **the public price less the import tariff**, and the
saving is therefore

    kWh into the car x (EV_PUBLIC_CHF_PER_KWH - that year's import tariff)

with no allocation anywhere in it. Which is a relief, because the
allocation is the part that cannot be done honestly: at the sample level
the house is drawing from the roof, the battery and the grid at once, and
any rule for deciding which of them the car got is a convention. The
dashboard still reports the split - 39% off the roof or the battery over
the first 120 kWh - because it says something true about when the car
charges. It just is not what the money rests on.

What measures it: a Shelly 3EM clamp on the charger's feed since 31 Oct
2026, read every tick like any other load. Before that date the only
witness was the charger's own cumulative register, because its power
reading cannot be integrated - it is polled on a slow clock and not at all
while a car is drawing, so the same snapshot got repeated into every row
and overstated the energy by a factor of 2.5. Both eras are in the figure
and every sample says which one it is from, so 119.8 kWh splits into 12.9
measured and 106.9 reconstructed from the counter's increments.

The split itself is now drawn, three ways rather than two: what the roof
put into the house directly, what the battery gave back, and what was
bought, attributed pro rata over the house's whole supply at each
sample. Over the first 132 kWh that is 18% / 20% / 62% - and per day it
is not noise but a pattern, because a charge at noon runs 82-100%
self-supplied and a charge at night runs 0%. The wallbox page has it as
a stacked bar per day, and every session in the list carries its own
mix. One subtlety it exposed: the clamp on the charger's feed sees the
charger's own 4-7 W standby, which is 0.14 kWh a day and was being
counted as energy that went into a car. The session list already had a
threshold for that; the split uses the same one.

The one thing here that is not a measurement is the 0.85. It is a
counterfactual - what the charging would otherwise have cost - and the
honest version of it depends on what the alternative really was. A 10 A
socket in the garage at the house tariff is a cheaper alternative than a
public charger, and to the extent that it was available, the wallbox's own
saving is smaller than this. That is why the price is a setting and not a
constant, and why `ev_charging` has to be named in
`FINANCE_RETURN_STREAMS` before it counts for anything.

### 21. One payback was the wrong number of paybacks

Counting the car's savings raised an obvious objection: the charger and
the roof are independent, and blending them makes one rate that describes
neither. The objection is right, and the arithmetic of section 20 is what
proves it - the saving is `public price - tariff`, which contains no PV
term at all. Every franc of it comes from owning a charger; the roof's
share of the charging was already in the solar stream at the tariff.

Blended, the two look like this:

| | Net | Rate | Break-even |
|---|---|---|---|
| PV, battery, heater | 22'952 | 6.74/day | ~Dec 2035 |
| Wallbox | 2'000 | 4.78/day | ~Dec 2027 |
| Together | 24'952 | 11.52/day | Aug 2032 |

The combined row is not wrong, it is uninformative: it reports a
mediocre investment and an excellent one as one middling one, and the
middling figure is the flattering one for the expensive half. The two
also carry different risks - one rate depends on sunshine, the other on
kilometres driven - so a single number is one risk profile short.

So the card shows three, one row each, and `FINANCE_EXTRA_STREAMS` says
which savings belong to `INVESTMENT_EXTRA_CHF` rather than to the
invoice. The grant and the tax deduction come off the invoice's side,
because that is what they were given for.

The rows are plain HTML, not SVG. A bar that is one number wide needs no
scale, no axis and no redraw on resize, and three stacked rows of
name / track / figures read in one pass where three charts would not.
Everything that used to be prose above the fold - the benchmark's
caveats, the tariff history, the per-stream basis - moved into the table
below it. The card is a third of its old height and says more.

### 22. Kilowatt-hours are not a unit anybody drives in

The charging figure is measured. What it means is not: nobody thinks in
kilowatt-hours, they think in kilometres and in what the old car would
have cost over them. So the wallbox page converts twice, and prints both
conversions next to the answer instead of hiding them in a config file.

How far a kWh goes depends on the month, and the factors are derived
rather than guessed:

* EV Database's figures for this car - a Smart #5 with one motor -
  238 Wh/km combined at -10 °C with the heating on and 179 Wh/km at
  23 °C with the cooling off. Linear between the two is 1.79 Wh/km per
  kelvin, and nothing is added above 23 °C - cold costs far more than
  heat.
* MeteoSwiss monthly mean temperatures for Zurich, 1991-2020 normals.

That gives 4.16 km/kWh in January against 4.88 in July - 17% apart,
where the figure usually quoted is the -10 °C worst case of 28%. The
sanity check: owner logs for a smaller EV show 16% between summer and
winter tyres. The model lands in the same place, which is where a Swiss
winter actually is - January's mean here is +0.9 °C, not -10.

Two numbers had to be separated to get this right. The car says 5
km/kWh and EV Database puts its yearly average at 202 Wh/km, which is
4.94 - so the car's figure is energy out of the battery, and the clamp
measures energy into the charger. `EV_CHARGING_LOSS_PCT` is the gap,
and the curve is drawn wall-side at 4.50 km/kWh, because that is the
side the money is on.

Scaling it to a year is done on kilometres and not on kilowatt-hours.
The same journey needs 15% less energy in July than in January, so
holding the kWh constant would bake whatever weather the measured
window happened to have into all twelve months. The twelve are the
twelve ahead, each priced at the tariff that will apply to it. Two
inputs stay at their measured value rather than being modelled - the
share that has to be bought, which summer will improve, and the petrol
side's lack of a season - and both of those errors point the same way.

It is still a floor. A monthly mean is not a driving temperature, and a
commute happens at the cold end of the day; and a short winter trip pays
for heating the cabin over fewer kilometres than a long one. The twelve
factors are `EV_MONTHLY_EFFICIENCY`, to be replaced with measured ones
once there is a winter's worth of them.

The one input here that is neither measured nor stable is the pump
price, and it was quietly the weakest number on the card: a single
figure with no date on it, multiplied by a year's litres. Bleifrei 95
went 1.87 in July 2026, past 2.00 in August, 2.13 in October and 1.99
on 6 November - 14% inside four months. So `FUEL_CHF_PER_LITRE` now
travels with `FUEL_PRICE_AS_OF` and an optional `FUEL_PRICE_RANGE`, the
card quotes the band rather than a point, and it states the
sensitivity: every 10 rappen on the litre is about 120 francs a year.
Nothing fetches the price. Scraping a motoring club's page hourly to
refine a twelve-month estimate would be precision theatre, and the
estimate's real error bar is the fortnight of driving underneath it.

Two things this deliberately does not do. The petrol side carries no
season, although a cold engine also drinks more - which makes the
comparison the conservative one. And it is not a finance stream: the
payback already values the charging against a public charger, and one
car cannot be compared with two alternatives at the same time without
counting the same francs twice. The card says so under its table.

### 23. A second power plant the first one cannot see

A 600 W balcony unit went up, its inverter plugged into a myStrom WiFi
switch. The question it raised is not how to read a plug - that took
twenty lines - but where its kilowatt-hours belong, and the instinct
to add them to the PV figure is wrong twice over.

**The plug is the only witness.** The balcony is on a socket, not on one
of the inverter's MPPT inputs, so `pv_kw` contains nothing of it and
never will. It gets its own column, its own page and its own money, and
the one thing never done to it is folding it into the roof's.

**What the balcony does to self-consumption: nothing, and that is a
result rather than an omission.** The inverter's current transformer
sits at the house connection point, upstream of every socket. A balcony
inverter downstream of it lowers the measured grid import and the
inverter's computed house load by exactly the same amount. Self-
consumption is the difference between those two, so it comes out
unchanged and already correct for the roof alone. Adding the balcony to
it would have been the double count - the money would have gone up with
no extra kWh behind it.

**What it does to export: everything, and that needed measuring.** The
house has one export meter and two plants behind it. A kWh the balcony
pushed through it earns the same rappen whichever panel made it, but it
pays off a different purchase. So the balcony's share is taken per
sample as `MIN(balcony_kw, MAX(grid_kw, 0))` and subtracted from the
roof's export credit. Per sample and never per bucket: a day that
imported for twenty hours and exported for four would otherwise show
either none of it or all of it, depending which way the net went.

The remainder displaced a purchase and is worth the buying price - more
than twice what the exported half earns here - so the split is the
difference between a right answer and a wrong one, not a nicety.

**The plug's own energy counter is not used.** `/report` carries a `Ws`
field that looks like one and is not: it is the average per second since
*the last call*, so it depends on the polling interval and is consumed
by whoever read it last. With Home Assistant polling the same plug, both
totals would quietly come up short. `energy_since_boot` is a real
counter but restarts whenever the plug loses mains, which a balcony
inverter's plug does often; it is recorded with its `boot_id` as a
cross-check and nothing more. The energy that counts is integrated from
`power`, like the house load and the heater.

**And the plug cannot tell direction.** It reports an unsigned
magnitude: 600 W out of an inverter and 600 W into a heater look
identical to it. Nothing in the reading establishes which this is -
setting `MYSTROM_BALCONY_HOST` is the statement that the inverter is
what is plugged in, and the page says so rather than implying the
instrument knows.

None of this could be checked against daylight on the day it was
written, so `tests/test_balcony_split.py` simulates a day at the real
70-second tick with the house crossing zero several times, and compares
the SQL against the same rule implemented in Python. It asserts that all
three branches - importing, exporting more than the balcony made,
exporting less - actually occurred, because a test that only exercises
one of them has not tested the split.

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
Description=Home Energy Manager
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

## Which roof side produces what

The inverter reports each MPPT input separately - four registers per input
from 35103: voltage, current, and power as a 32-bit word. `pv_kw` sums the
four, and for a long time that sum was all this kept, which threw away the
one thing the registers can say about the roof.

Two inputs are wired up here, one roof side each. Measured at 10:38 on 30
Oct 2026:

    PV1   530.1 V   1.1 A   634 W
    PV2   655.3 V   1.0 A   675 W
    PV3     0.0 V   0.0 A     0 W
    PV4     0.0 V   0.0 A     0 W

The two string voltages differ by a quarter, which is the module count
differing by a quarter: the sides are not the same size. The currents are
almost equal, so each string is a single series run.

**Which input is on which side is in no register.** It has to be read off
the daily curve, and the curve says it plainly: the side that peaks before
solar noon faces east, the one that peaks after it faces west. Two sides
that peak together are both facing south, differing only in pitch. That is
why the labels come from `PV_STRING_NAMES` rather than from the code, and
why they default to naming nothing. The number of entries in that setting
is also how many inputs the dashboard shows - how many are wired up is a
fact about the roof, not something to infer from a night-time reading of
zero.

**The two sides here face north-east and south-west.** Which input is on
which was not readable from the morning that this was first recorded: 1.2
to 1.9 kW out of a 15 kW plant is a fully overcast sky, and under diffuse
light alone the orientation hardly matters - over ten samples the longer
string took 54% of the output while carrying 55% of the modules, so the two
were level per module and said nothing about where they point.

The afternoon of the same day settled it. It cleared up, and with direct
sun the two sides stopped behaving alike:

    Hour    PV1        PV2        PV2 share
    12:00   1.190 kW   1.932 kW   62%
    13:00   0.781 kW   4.588 kW   85%
    14:00   0.607 kW   1.905 kW   76%

    Peak    PV1  1.371 kW at 12:21     PV2  8.565 kW at 13:41
    Day     PV1  3.89 kWh (28%)        PV2  10.14 kWh (72%)

PV1 peaks before solar noon and falls away from then on; PV2 is still
climbing at that point and makes six times as much an hour later. The
south-west share goes from 59% before solar noon to 78% after it, and a
north-east roof cannot gain as the sun moves south. So PV1 is the
north-east side and PV2 the south-west one, recorded as
`PV_STRING_NAMES=North-East,South-West`.

The 28/72 split is harder than the module count alone would be - PV2
carries about a quarter more modules but made two and a half times the
energy. That is late October: a north-east roof only catches direct sun in
the first hours of the morning, and those hours were overcast. The split
narrows towards midsummer.

The inverter keeps **no energy counter per input**, only per plant, so
"which side produced how much today" cannot be read off a register. It has
to be integrated from the recorded power, which is why `pv1_kw..pv4_kw` are
columns in the history rather than only numbers on the dashboard, and why
the energy page sums them itself per bucket. Rows recorded before those
columns existed hold NULL, not zero, and the page shows that production as
a neutral segment labelled as having no split rather than letting the two
sides stand in for a total they do not add up to.

## Known limitations

- Local Modbus/TCP is not enabled on a GoodWe data logger by default and
  cannot be switched on from the outside - GoodWe support has to enable it
  per device (see problem #2). Without that, this approach does not work at
  all, and the cloud API is not a usable substitute.
- The heater is regulated one step per poll, so it takes a few minutes to
  work its way up to the right step after a change in conditions. That is
  deliberate - the steps switch mechanical relays in the AskoHeat.
- The break-even forecast is a straight-line projection of a daily rate
  measured over a few months, and it inherits every assumption behind the
  oil conversion below. It is an order of magnitude, not a date to plan
  around, until there is a full year in the database.
- The litres of oil beside the heat pump's kWh are an estimate, not a
  measurement: nothing in this installation measures heat, only the
  electricity going into the pump. The seasonal performance factor is a
  setting (`HEATPUMP_SPF`), and until a heat meter or a full winter of data
  exists it stays one.
- The heat pump's controller cannot be read or written at all through its
  OCI670 gateway, so everything the dashboard shows about it is inferred
  from a clamp meter on its feed: when the compressor ran and what it drew,
  never why. Whether a run was heating or a hot-water charge has to be read
  off the shape of the record or off the machine's own display.
- Register addresses and the value-derivation formulas above are specific to
  this particular installation (GoodWe GW15K-ETA-G20 hybrid inverter +
  AskoHeat+) and firmware version, and may not generalize to other models.
- `.env` holds the addresses of your own devices and is git-ignored - never
  commit it.

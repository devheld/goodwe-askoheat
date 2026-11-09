"""Prove the balcony/export split against an independent implementation.

No daylight has happened yet with the plug fitted, so the split is tested
against a simulated day instead: a full day of samples at the real 70 s
tick, the balcony following a sine over the daylight hours, and the house
crossing between import and export several times - which is the only
condition in which the split does anything at all.

The expectation is computed in Python from the same per-sample rule the
SQL claims to implement, so this compares two implementations rather than
the SQL against a number somebody typed. The three branches the day has
to contain, and the test checks that it did:

  house importing          all of the balcony displaced a purchase
  export >= balcony        all of it went out
  export <  balcony        the difference stayed in the house

The third is the interesting one: the house exported only because the
balcony was running, and without it would have imported.
"""

import math
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import MAX_SAMPLE_SPAN_H, HistoryStore, Reading  # noqa: E402

db = Path(tempfile.gettempdir()) / "balcony_split_test.db"
if db.exists():
    db.unlink()
store = HistoryStore(db)

TICK_S = 70
NOMINAL_KW = 0.6

# Local midnight, so the whole simulated day lands in today's buckets.
now = datetime.now().astimezone()
midnight_local = now.replace(hour=0, minute=0, second=0, microsecond=0)
start = midnight_local.astimezone(timezone.utc)

samples = []
t = start
while t < min(start + timedelta(hours=24), datetime.now(timezone.utc)):
    local = t.astimezone()
    hour = local.hour + local.minute / 60.0
    # Daylight 08:00-18:00, a half sine over it.
    balcony = (
        round(NOMINAL_KW * math.sin(math.pi * (hour - 8) / 10.0), 3)
        if 8 <= hour <= 18
        else 0.0
    )
    # A house that swings either side of zero through the day, so every
    # branch is exercised: import at night, export around noon, and a
    # stretch where the export is smaller than what the balcony is making.
    grid = (
        round(2.0 * math.sin(math.pi * (hour - 7) / 11.0) - 0.9, 3)
        if 7 <= hour <= 18
        else -1.4
    )
    samples.append((t, grid, balcony))
    t += timedelta(seconds=TICK_S)

if len(samples) < 60:
    print("not enough of today has passed to simulate; run again later")
    sys.exit(1)

for ts, grid, balcony in samples:
    store.insert(
        Reading(
            timestamp=ts.isoformat(),
            pv_kw=0.0,
            consumption_kw=1.0,
            battery_charge_kw=0.0,
            battery_discharge_kw=0.0,
            grid_kw=grid,
            battery_charging=False,
            battery_soc_pct=50.0,
            step=0,
            heater_load_w=0,
            askoheat_temp_c=40.0,
            status="idle",
            status_text="test",
            balcony_kw=balcony,
        )
    )

# ------------------------------------------------ the same rule, in Python
made = exported = kept = house_export = 0.0
branches = {"importing": 0, "export>=balcony": 0, "export<balcony": 0}
prev_ts = None
for ts, grid, balcony in samples:
    h = (
        0.0
        if prev_ts is None
        else min((ts - prev_ts).total_seconds() / 3600.0, MAX_SAMPLE_SPAN_H)
    )
    prev_ts = ts
    grid_export = max(grid, 0.0)
    share = min(balcony, grid_export)
    made += balcony * h
    exported += share * h
    kept += (balcony - share) * h
    house_export += grid_export * h
    if balcony > 0:
        if grid_export == 0:
            branches["importing"] += 1
        elif grid_export >= balcony:
            branches["export>=balcony"] += 1
        else:
            branches["export<balcony"] += 1

out = store.energy_buckets("today")
tot = out["totals"]
print("samples %d over %.2f h; branches: %s" % (len(samples), tot["hours"], branches))
print(
    "store : made %.3f  exported %.3f  kept %.3f  house export %.3f"
    % (tot["balcony"], tot["balcony_export"], tot["balcony_self"], tot["grid_export"])
)
print(
    "python: made %.3f  exported %.3f  kept %.3f  house export %.3f"
    % (made, exported, kept, house_export)
)

# The totals are stored rounded to 3 decimals; half of the last digit is
# the tightest an honest comparison can be.
TOL = 5.1e-4
ok = True
for name, got, want in (
    ("made", tot["balcony"], made),
    ("exported", tot["balcony_export"], exported),
    ("kept", tot["balcony_self"], kept),
    ("house export", tot["grid_export"], house_export),
):
    good = abs(got - want) < TOL
    ok &= good
    print("  %-13s %.4f vs %.4f  %s" % (name, got, want, "OK" if good else "WRONG"))

# kept is derived per bucket as made - exported, so the identity is exact
# before rounding and holds to the stored precision after it: three totals
# each rounded to 3 decimals can disagree in the last digit, which is why
# this is checked at that precision and not at 1e-6. Both figures are
# printed to 2 decimals on the page, where they add up.
identity = abs((tot["balcony_self"] + tot["balcony_export"]) - tot["balcony"]) < 1.1e-3
ok &= identity
print(
    "  identity      kept + exported == made to the stored precision  %s"
    % ("OK" if identity else "WRONG")
)

roof = tot["grid_export"] - tot["balcony_export"]
good = roof >= -1e-9
ok &= good
print(
    "  roof export   %.4f (house %.4f less the balcony's %.4f)  %s"
    % (roof, tot["grid_export"], tot["balcony_export"], "OK" if good else "WRONG")
)

# All three branches have to have happened, or the test proved nothing
# about the two that did not.
covered = all(branches.values())
ok &= covered
print("  coverage      all three branches hit  %s" % ("OK" if covered else "WRONG"))

# Windows keeps the file open until the connections are collected, and a
# leftover scratch database is not a test failure.
try:
    db.unlink()
except OSError:
    pass

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)

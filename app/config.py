from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent


DEFAULT_MONTHLY_SHARES = (3.0, 5.0, 8.0, 10.5, 12.0, 12.5, 13.0, 11.5, 9.0, 6.5, 3.5, 2.5)


def _monthly_shares() -> tuple[float, ...]:
    """Twelve proportional shares of the annual PV yield, January first.

    Normalised to sum to 1, so any twelve positive numbers will do.
    Anything else falls back to the default profile with a warning: a
    malformed list must not silently become a flat year, which is the
    error the profile exists to prevent.
    """
    raw = os.environ.get("BENCHMARK_MONTHLY_SHARES", "").strip()
    shares = DEFAULT_MONTHLY_SHARES
    if raw:
        try:
            parsed = tuple(float(p) for p in raw.split(","))
            if len(parsed) != 12 or sum(parsed) <= 0 or min(parsed) < 0:
                raise ValueError(parsed)
            shares = parsed
        except ValueError:
            logging.getLogger(__name__).warning(
                "BENCHMARK_MONTHLY_SHARES: need 12 non-negative numbers, using default"
            )
    total = sum(shares)
    return tuple(s / total for s in shares)


def _tariffs() -> tuple[tuple[int, float, float], ...]:
    """Parse TARIFFS into (year, import, export), oldest first.

    Format: `2026:0.28:0.11,2027:0.25:0.10` - the year the rate applies
    to, the import price, the export price. A year and not a date, because
    the utility publishes once a year and a date field would invite
    guessing at a day that does not exist. An unparsable entry is dropped
    with a warning rather than taking the whole configuration down: a typo
    in a price list should not stop the heater from regulating.
    """
    raw = os.environ.get("TARIFFS", "").strip()
    entries: list[tuple[int, float, float]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        fields = part.split(":")
        if len(fields) != 3:
            logging.getLogger(__name__).warning("TARIFFS: ignoring %r", part)
            continue
        try:
            year = int(fields[0].strip())
            if not 1900 <= year <= 2200:
                raise ValueError(year)
            entries.append((year, float(fields[1]), float(fields[2])))
        except ValueError:
            logging.getLogger(__name__).warning("TARIFFS: ignoring %r", part)
    return tuple(sorted(entries, key=lambda e: e[0]))


def _finance_return_streams() -> tuple[str, ...]:
    """Which measured savings are allowed to pay off the investment.

    A list rather than a flag because the answer is a fact about the
    invoice, not a preference: if the heat pump was bought separately, its
    savings belong to a different sum.
    """
    raw = os.environ.get("FINANCE_RETURN_STREAMS", "pv_self,pv_export,heating")
    names = tuple(part.strip() for part in raw.split(",") if part.strip())
    return names or ("pv_self", "pv_export", "heating")


@dataclass(frozen=True)
class Settings:
    goodwe_host: str
    goodwe_port: int
    goodwe_slave_id: int

    askoheat_host: str
    askoheat_port: int
    askoheat_step_register: int
    askoheat_load_register: int
    askoheat_temp_register: int
    askoheat_slave_id: int
    askoheat_delay_s: int

    # GoodWe AC EV charger (2nd gen). Read-only and entirely optional - the
    # dashboard shows it when it answers and says so when it doesn't. Set
    # WALLBOX_HOST empty to leave it out altogether.
    wallbox_host: str
    wallbox_port: int
    wallbox_slave_id: int
    # Polled on its own, slower clock than the heating loop: the
    # charger has one free Modbus slot and the owner's app needs it
    # too, so this reader takes it briefly and rarely.
    wallbox_poll_interval_s: int
    # Two ways to answer "is a car drawing right now", which is the one
    # question that decides whether the charger may be read. The clamp meter
    # on its feed answers it directly and is used whenever it is there;
    # wallbox_busy_load_kw is the old answer, inferred from the household
    # load, and stays as the fallback for a meter that is away.
    wallbox_meter_busy_kw: float
    wallbox_busy_load_kw: float
    wallbox_idle_ticks: int
    wallbox_idle_poll_interval_s: int
    # Which of the two reading strategies runs. "gated" leaves the charger
    # alone while the household load says a car is drawing; "direct" reads
    # it on the interval whatever the house is doing, which is how this ran
    # before the fault was found. Kept switchable because the fault is the
    # charger's own, so a firmware change can end it - and the only way to
    # find out is to read again and watch whether a charge survives.
    wallbox_poll_mode: str

    # Shelly 3EM clamp meters, HTTP and read-only. Three questions, three
    # devices, each one optional: an empty host leaves that meter out.
    #   grid     - the three main phases. An independent witness to the
    #              connection point and the only instrument here that
    #              measures the current through the main fuse.
    #   heatpump - the heat pump's feed. The largest load in the house and
    #              the one nothing else measures at all.
    #   wallbox  - the charger's feed. This one is not about curiosity: it
    #              makes the charger's own Modbus registers unnecessary
    #              while a car is drawing, which is the whole of section 7.
    # What a device is for is the name it is configured under, not a setting
    # on the device. It decides the sign and the columns - see ShellyClient
    # and Reading in database.py.
    shelly_grid_host: str

    # The main fuse, in amps per phase, and the share of it that counts as
    # getting close. The clamp meter at the house connection is the only
    # instrument here that can see phase current - the inverter reports
    # load per phase but not what the connection carries - and it has
    # already recorded 22 A on a single phase, which nothing displayed.
    # Nothing here protects anything: the fuse does that. This only makes
    # the distance to it visible before a new load is added.
    main_fuse_a: float
    main_fuse_warn_pct: float
    shelly_heatpump_host: str
    shelly_wallbox_host: str
    shelly_port: int
    shelly_timeout_s: float

    # What the heat pump draws flat out, for the chart's y axis. A curve
    # measured against the machine's own ceiling says how hard it is
    # working; one rescaled to its own maximum every time only says what
    # happened to be the busiest minute on screen. 0 turns it off and the
    # axis follows the data again. It never clips: a reading above the
    # ceiling pushes the axis up rather than disappearing.
    heatpump_max_kw: float

    # The hours the pump is meant to heat in, on the local clock, and what
    # counts as its compressor running.
    #
    # Neither is a control. Nothing here can switch this machine: its
    # gateway speaks no Modbus and the controller has no other interface.
    # The window is the line the record gets measured against, so "it ran at
    # three in the morning" becomes a row with a timestamp instead of
    # something somebody heard.
    #
    # 0.5 kW sits in the gap between the three states the clamp actually
    # sees on this machine: the controller alone draws 0.035 kW, a
    # circulation pump on its own 0.13-0.14 kW, and the compressor
    # 1.10-1.21 kW. So it separates "heating" from "idling" and from
    # "circulating", which is the distinction the runs are about.
    heatpump_window_from_h: int
    heatpump_window_to_h: int
    heatpump_run_kw: float

    # The heat pump's electricity expressed in the unit this house used to
    # be heated in. Nothing here is measured: the clamp meter sees
    # electricity going in and never heat coming out, so the conversion is
    # three assumptions, kept in configuration so the page can print them
    # next to the number and a better figure can replace them without
    # touching code.
    #
    # heatpump_spf: heat delivered per kWh of electricity across a season.
    #   The datasheet for this machine (CTA Optiheat 1-5es, brine/water)
    #   gives COP 4.5 at B0/W35, 3.4 at B0/W45 and 2.7 at B0/W55, with
    #   1.1/1.4/1.7 kW drawn - and the clamp reads 1.10-1.21 kW, which is
    #   the B0/W35 column. 4.0 is deliberately below that: over a winter
    #   the brine gets colder and hot water needs a higher flow
    #   temperature, and a lab SCOP is not a seasonal figure.
    # oil_kwh_per_litre: the energy in a litre of heating oil, 10 kWh
    #   (35.9 MJ) by its lower heating value.
    # oil_boiler_efficiency: how much of that an old non-condensing boiler
    #   actually put into the house across a year, flue and standby losses
    #   included. 0.85 is the optimistic end for the oil heating, which is
    #   the fair direction to err in for a comparison it is going to lose.
    heatpump_spf: float
    oil_kwh_per_litre: float
    oil_boiler_efficiency: float
    oil_price_chf_per_litre: float

    # What the installation cost, and what counts as earning it back. These
    # amounts live in .env and never in the repository: an invoice is the
    # household's business. .env.example carries zeros and the explanation.
    #
    # investment_chf: the invoice total, gross, before anything came back.
    #   Zero means "not entered yet", and the page says so rather than
    #   reporting a break-even it cannot know.
    # subsidy_chf: the grant actually paid out.
    # tax_deduction_chf / tax_deduction_rate: a deduction is not cash. The
    #   first is the amount that may be deducted, the second the share of
    #   it that became money - the marginal tax rate. At 1.0 the deduction
    #   is taken at face value, which is the optimistic reading of it.
    # investment_date: when the installation started earning, if that is
    #   earlier than the first recorded sample. Only the recorded part can
    #   be counted, but knowing the real date lets the page admit what it
    #   is missing instead of starting the clock where the database does.
    # finance_return_streams: which of the measured savings count against
    #   the investment. Heating-instead-of-oil belongs there only if the
    #   heat pump was on the same invoice.
    investment_chf: float
    # Real money that is not on the invoice. The wallbox was installed and
    # paid for outside the solar contract, and leaving it out would flatter
    # the payback while its charging sits in the savings - the car is the
    # largest single reason self-consumption is above the quoted plan, so
    # the cost of being able to charge it belongs on the same page.
    investment_extra_chf: float
    investment_extra_label: str
    subsidy_chf: float
    tax_deduction_chf: float
    tax_deduction_rate: float
    investment_date: str
    finance_return_streams: tuple[str, ...]

    # The yardstick: what the installation was sold as doing. Taken from
    # the quote that led to the order, so the dashboard can answer "is it
    # doing what it was supposed to" instead of only "how much so far".
    # The amounts are in .env with the rest of the money.
    #
    # The quote's own economic model is a straight line - a fixed annual
    # return from year one - and it is used here as the straight line it
    # is, rather than being given a seasonality the document never had.
    # For this installation that costs almost nothing: weighting the
    # quote's annual yield by a Swiss monthly profile over the recorded
    # window changes the comparison by about one percentage point, because
    # mid-August to the end of October happens to sit within two percent
    # of the annual daily mean.
    #
    # benchmark_self_kwh_per_year is what the quote expected the house to
    # use itself; the remainder of the annual yield is what it expected to
    # be exported, and the two are priced at the quote's own tariffs. That
    # split is the whole reason to keep the quote's prices separate from
    # the ones in PRICE_*: the comparison has to be made at the prices the
    # promise was made at.
    benchmark_label: str
    benchmark_kwp: float
    benchmark_kwh_per_year: float
    benchmark_self_kwh_per_year: float
    benchmark_import_chf_per_kwh: float
    benchmark_export_chf_per_kwh: float
    benchmark_payback_years: float
    benchmark_net_chf: float

    # One label per connected MPPT input, in order. How many entries there
    # are is how many inputs the dashboard shows, because how many are
    # wired up is a fact about the roof and not something to infer from a
    # night-time reading of zero. Which roof side an input is on is in no
    # register either: it has to be read off the daily curve, and the side
    # that peaks before noon faces east. Until that is settled the default
    # names claim nothing.
    pv_string_names: tuple[str, ...]

    step_min: int
    step_max: int
    poll_interval_s: int
    zero_threshold_kw: float
    # Hysteresis: a new heating cycle (step 0 -> >0) is only started while the
    # AskoHeat's tank temperature is at or below this. Once heating is
    # already running it is left to the device's own (higher) temperature
    # limit rather than stopped here - see SurplusController._tick().
    heating_start_max_temp_c: float
    # Battery priority: heating only starts once the battery has reached
    # battery_full_soc_pct, and a running heating cycle is stopped again when
    # the SOC falls below battery_keep_heating_soc_pct. The gap between the
    # two is the hysteresis that keeps the heater from switching on and off
    # around a single threshold - the heater's own load is what pushes the
    # SOC back down in the first place.
    battery_full_soc_pct: float
    battery_keep_heating_soc_pct: float
    # Only relevant while the grid connection actually allows export (this
    # installation is at 0% feed-in, see README): with the battery full and
    # power being exported, another heater step is added once at least this
    # much is going to the grid. The AskoHeat's smallest step is ~600W, so a
    # smaller margin would overshoot into grid import.
    surplus_step_margin_kw: float

    # Tariff in CHF per kWh. Display only - the control loop never reads
    # these, it regulates on power, not on price.
    price_import_chf_per_kwh: float
    price_export_chf_per_kwh: float
    # The tariff history by year, oldest first. Prices change once a year,
    # and a counter that spans a change has to price each kWh at the
    # tariff that was in force when it was used - otherwise every new
    # tariff sheet silently rewrites last year's savings. PRICE_* above
    # stay as the fallback for years before the first entry.
    tariffs: tuple[tuple[int, float, float], ...]

    # The date feed-in was permitted from, and the only date from which
    # this installation's output means anything.
    #
    # Before it the inverter was held to the house's own consumption. It
    # did not merely lose the export revenue the quote counted on - it was
    # never allowed to show what the roof can do, because the moment
    # production would have exceeded the load and a full battery, it was
    # throttled. So the recorded production for those months is a ceiling
    # imposed by a permit, not a measurement of a roof, and no comparison
    # with a predicted annual yield can be drawn from it in either
    # direction. The measured savings from that time are real money and
    # stay in the counter; the benchmark simply starts later.
    #
    # Blank means there is no valid comparison window yet, and the card
    # says so instead of computing a ratio out of throttled months.
    export_allowed_from: str

    # How many recorded days inside that window before ratios are shown at
    # all. A percentage from four days of November is noise with a decimal
    # point, and a benchmark that prints one teaches the reader to ignore
    # it.
    benchmark_min_days: float

    # How a year's yield is spread over the months, as twelve shares. The
    # quote states one annual figure, and a window that is November only
    # cannot be compared against a twelfth of it: November is dark, and
    # the ratio would read as a fault in the roof. So the quoted annual
    # energies are distributed over the months by this profile before any
    # part of them is compared with anything.
    #
    # This is the one assumption in the benchmark that is not from the
    # quote. It is normalised on load, so the numbers only have to be
    # proportional; the default is a Swiss Mittelland profile. Replacing
    # it with figures for the actual roof - PVGIS will give them per
    # orientation - makes the monthly comparison sharper, and once a full
    # year is recorded the profile stops mattering at all.
    benchmark_monthly_shares: tuple[float, ...]

    # Usable battery capacity in kWh, needed to tell a round-trip loss from
    # energy that is simply still in the battery. Not readable over Modbus
    # here, so it is configuration: derived for this installation from five
    # grid charges that ran from a known SOC to a known SOC (25.6 / 27.1 /
    # 27.5 / 28.3 / 31.8 kWh implied).
    battery_capacity_kwh: float

    db_path: Path


WALLBOX_POLL_MODES = ("gated", "direct")


def _wallbox_poll_mode() -> str:
    """Read WALLBOX_POLL_MODE, falling back to the one that protects a charge.

    A typo must not quietly turn the protection off, so anything
    unrecognised becomes "gated" and says so in the log.
    """
    raw = os.environ.get("WALLBOX_POLL_MODE", "gated").strip().lower()
    if raw in WALLBOX_POLL_MODES:
        return raw
    logging.getLogger(__name__).warning(
        "WALLBOX_POLL_MODE=%r is not one of %s - falling back to 'gated'",
        raw,
        ", ".join(WALLBOX_POLL_MODES),
    )
    return "gated"


PV_STRING_COUNT_DEFAULT = 2


def _pv_string_names() -> tuple[str, ...]:
    """Read PV_STRING_NAMES: one comma-separated label per connected input.

    The count is meaningful, not just the labels - two entries means two
    inputs are wired up, which is what this plant measures (PV1 and PV2
    carry voltage, PV3 and PV4 read 0.0V). An empty setting falls back to
    that count with neutral names, so the dashboard is right about how many
    sides there are before it is right about what they are called.
    """
    given = [n.strip() for n in os.environ.get("PV_STRING_NAMES", "").split(",")]
    given = [n for n in given if n][:4]
    if not given:
        given = ["String %d" % (i + 1) for i in range(PV_STRING_COUNT_DEFAULT)]
    return tuple(given)


def load_settings() -> Settings:
    return Settings(
        goodwe_host=os.environ.get("GOODWE_HOST", "192.168.68.50"),
        goodwe_port=int(os.environ.get("GOODWE_PORT", "502")),
        goodwe_slave_id=int(os.environ.get("GOODWE_SLAVE_ID", "247")),
        askoheat_host=os.environ.get("ASKOHEAT_HOST", "192.168.68.73"),
        askoheat_port=int(os.environ.get("ASKOHEAT_PORT", "502")),
        askoheat_step_register=int(os.environ.get("ASKOHEAT_STEP_REGISTER", "200")),
        askoheat_load_register=int(os.environ.get("ASKOHEAT_LOAD_REGISTER", "110")),
        askoheat_temp_register=int(os.environ.get("ASKOHEAT_TEMP_REGISTER", "325")),
        askoheat_slave_id=int(os.environ.get("ASKOHEAT_SLAVE_ID", "1")),
        askoheat_delay_s=int(os.environ.get("ASKOHEAT_DELAY_S", "10")),
        wallbox_host=os.environ.get("WALLBOX_HOST", "192.168.1.172"),
        wallbox_port=int(os.environ.get("WALLBOX_PORT", "502")),
        wallbox_slave_id=int(os.environ.get("WALLBOX_SLAVE_ID", "247")),
        wallbox_poll_interval_s=int(
            os.environ.get("WALLBOX_POLL_INTERVAL_S", "300")
        ),
        # Power on the charger's own feed that means a car is drawing. The
        # charger's standby was measured here at 4-7W with nothing plugged
        # in, and the smallest charge it can deliver is a single-phase car at
        # the 6A minimum, 1.38kW. 0.1kW sits twenty times above the one and
        # fourteen times below the other, which is the whole reason the clamp
        # replaced the household-load estimate: that estimate had to be set
        # at 3.0kW to stay clear of a kettle, and so could not see a
        # single-phase charge at all.
        wallbox_meter_busy_kw=float(
            os.environ.get("WALLBOX_METER_BUSY_KW", "0.1")
        ),
        # Household load above the heater that means a car is drawing, and
        # therefore that the charger must not be touched. See
        # Controller._poll_wallbox. Above the house's base load (measured up
        # to 2.5kW here) and below a three-phase car at the 6A minimum
        # (4.14kW), so neither a kettle nor a slow charge is misread.
        wallbox_busy_load_kw=float(os.environ.get("WALLBOX_BUSY_LOAD_KW", "3.0")),
        # Ticks the load must stay low before a charge counts as over. A dip
        # of one tick is not the end of one: the longest pause measured here
        # was 2.3 minutes, so five ticks of 70s outlasts it with room to
        # spare, at the price of a first poll delayed by about six minutes.
        wallbox_idle_ticks=int(os.environ.get("WALLBOX_IDLE_TICKS", "5")),
        # Poll interval while a car is plugged in but taking nothing. Every
        # read is a chance to block it from resuming, and a parked car needs
        # no minute-by-minute detail.
        wallbox_idle_poll_interval_s=int(
            os.environ.get("WALLBOX_IDLE_POLL_INTERVAL_S", "1200")
        ),
        wallbox_poll_mode=_wallbox_poll_mode(),
        # SHELLY_HOST is the name the first of these meters had when it was
        # the only one, kept as a fallback so an older .env still works.
        main_fuse_a=float(os.environ.get("MAIN_FUSE_A", "0")),
        main_fuse_warn_pct=float(os.environ.get("MAIN_FUSE_WARN_PCT", "80")),
        shelly_grid_host=os.environ.get(
            "SHELLY_GRID_HOST", os.environ.get("SHELLY_HOST", "")
        ),
        shelly_heatpump_host=os.environ.get("SHELLY_HEATPUMP_HOST", ""),
        shelly_wallbox_host=os.environ.get("SHELLY_WALLBOX_HOST", ""),
        shelly_port=int(os.environ.get("SHELLY_PORT", "80")),
        # Short on purpose: this is a dashboard reading on the heating loop's
        # clock, and a meter that is slow to answer must not hold up the
        # heater decision behind it.
        shelly_timeout_s=float(os.environ.get("SHELLY_TIMEOUT_S", "3")),
        heatpump_max_kw=float(os.environ.get("HEATPUMP_MAX_KW", "0")),
        heatpump_window_from_h=int(os.environ.get("HEATPUMP_WINDOW_FROM_H", "8")),
        heatpump_window_to_h=int(os.environ.get("HEATPUMP_WINDOW_TO_H", "18")),
        heatpump_run_kw=float(os.environ.get("HEATPUMP_RUN_KW", "0.5")),
        heatpump_spf=float(os.environ.get("HEATPUMP_SPF", "4.0")),
        oil_kwh_per_litre=float(os.environ.get("OIL_KWH_PER_LITRE", "10.0")),
        oil_boiler_efficiency=float(os.environ.get("OIL_BOILER_EFFICIENCY", "0.85")),
        oil_price_chf_per_litre=float(os.environ.get("OIL_PRICE_CHF_PER_LITRE", "1.78")),
        investment_chf=float(os.environ.get("INVESTMENT_CHF", "0")),
        investment_extra_chf=float(os.environ.get("INVESTMENT_EXTRA_CHF", "0")),
        investment_extra_label=os.environ.get(
            "INVESTMENT_EXTRA_LABEL", "Not on the invoice"
        ).strip(),
        subsidy_chf=float(os.environ.get("SUBSIDY_CHF", "0")),
        tax_deduction_chf=float(os.environ.get("TAX_DEDUCTION_CHF", "0")),
        tax_deduction_rate=float(os.environ.get("TAX_DEDUCTION_RATE", "1.0")),
        investment_date=os.environ.get("INVESTMENT_DATE", "").strip(),
        finance_return_streams=_finance_return_streams(),
        benchmark_label=os.environ.get("BENCHMARK_LABEL", "").strip(),
        benchmark_kwp=float(os.environ.get("BENCHMARK_KWP", "0")),
        benchmark_kwh_per_year=float(os.environ.get("BENCHMARK_KWH_PER_YEAR", "0")),
        benchmark_self_kwh_per_year=float(
            os.environ.get("BENCHMARK_SELF_KWH_PER_YEAR", "0")
        ),
        benchmark_import_chf_per_kwh=float(
            os.environ.get("BENCHMARK_IMPORT_CHF_PER_KWH", "0")
        ),
        benchmark_export_chf_per_kwh=float(
            os.environ.get("BENCHMARK_EXPORT_CHF_PER_KWH", "0")
        ),
        benchmark_payback_years=float(os.environ.get("BENCHMARK_PAYBACK_YEARS", "0")),
        benchmark_net_chf=float(os.environ.get("BENCHMARK_NET_CHF", "0")),
        pv_string_names=_pv_string_names(),
        step_min=int(os.environ.get("STEP_MIN", "0")),
        step_max=int(os.environ.get("STEP_MAX", "7")),
        poll_interval_s=int(os.environ.get("POLL_INTERVAL_S", "60")),
        zero_threshold_kw=float(os.environ.get("ZERO_THRESHOLD_KW", "0.01")),
        heating_start_max_temp_c=float(os.environ.get("HEATING_START_MAX_TEMP_C", "55")),
        battery_full_soc_pct=float(os.environ.get("BATTERY_FULL_SOC_PCT", "95")),
        battery_keep_heating_soc_pct=float(
            os.environ.get("BATTERY_KEEP_HEATING_SOC_PCT", "90")
        ),
        surplus_step_margin_kw=float(os.environ.get("SURPLUS_STEP_MARGIN_KW", "0.6")),
        price_import_chf_per_kwh=float(
            os.environ.get("PRICE_IMPORT_CHF_PER_KWH", "0.25")
        ),
        price_export_chf_per_kwh=float(
            os.environ.get("PRICE_EXPORT_CHF_PER_KWH", "0.10")
        ),
        tariffs=_tariffs(),
        export_allowed_from=os.environ.get("EXPORT_ALLOWED_FROM", "").strip(),
        benchmark_min_days=float(os.environ.get("BENCHMARK_MIN_DAYS", "30")),
        benchmark_monthly_shares=_monthly_shares(),
        battery_capacity_kwh=float(os.environ.get("BATTERY_CAPACITY_KWH", "27")),
        db_path=PROJECT_ROOT / os.environ.get("DB_PATH", "data/home-energy.db"),
    )


settings = load_settings()

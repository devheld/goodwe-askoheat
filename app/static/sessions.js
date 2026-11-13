// Charging sessions, the charger's event log and the cross-check between
// the clamp meter and the charger's own registers - the wallbox page.
//
// None of it is read off the charger live: reading it while a car draws cuts
// the charging current (see the README), so the history comes from what was
// recorded. Since 31 Oct 2026 what was recorded is a measurement, from a
// clamp meter on the charger's feed; before that it is a reconstruction from
// the charger's counter, and every session says which it is.
//
// Needs chartkit.js and d3 before it.

const SESSION_DAYS = 30;
// Sessions end minutes apart at best, so this is about catching the one
// that finished while the page stayed open, not about being live.
const SESSION_REFRESH_MS = 5 * 60 * 1000;

// Where the charging came from. Three sources that add up to the day's
// charging, so it is one stacked bar per day and not three lines: the
// question is composition, and the total is worth reading off the same
// mark. Horizontal, because the label is a date and a date is wider than
// a bar is tall, and only days that saw a charge get a row - a 30-day
// axis with six bars on it would be mostly empty.
//
// Blue, amber, magenta are slots 1, 4 and 5 of the project's categorical
// theme, the same hues the energy page gives the roof, the battery and
// the grid, so the mapping is learned once. Validated as a stacked
// adjacent set against both surfaces; the 2px gap between segments is
// part of that, and the legend plus the table carry the identity that
// colour alone must not.
const SOURCE_PARTS = [
  { key: "pv", name: "Straight off the roof", color: "--series-pv" },
  { key: "battery", name: "Out of the battery", color: "--series-batt-discharge" },
  { key: "grid", name: "Bought from the grid", color: "--series-grid" },
];

// One chart for both views, so the two can never disagree: a row is a
// day or a session, and either way it is split by source and labelled
// with its kWh and what the bought part cost.
let chargeView = "day";

function importRate(src) {
  return (src && src.import_chf_per_kwh) || chartTariff.import_per_kwh || 0;
}

function chargeRows(runs) {
  const src = runs.sources || {};
  if (chargeView === "session") {
    return (runs.sessions || []).slice(0, 20).map((s) => {
      const start = new Date(s.start);
      return {
        label: start.toLocaleString("en-GB", {
          day: "2-digit",
          month: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
        }),
        title:
          start.toLocaleString("en-GB") +
          ` · ${fmt(s.duration_min, 0)} min · peak ${fmt(s.peak_kw, 1)} kW` +
          (s.measured ? "" : " · reconstructed"),
        pv: s.pv_kwh || 0,
        battery: s.battery_kwh || 0,
        grid: s.grid_kwh || 0,
        kwh: s.energy_kwh || 0,
      };
    });
  }
  // Newest first, same as the session view.
  return (src.by_day || [])
    .filter((d) => d.kwh > 0.05)
    .slice(-20)
    .reverse()
    .map((d) => {
      const when = new Date(`${d.day}T12:00:00`);
      return {
        label: when.toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "2-digit" }),
        title: when.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" }),
        pv: d.pv || 0,
        battery: d.battery || 0,
        grid: d.grid || 0,
        kwh: d.kwh || 0,
      };
    });
}

function renderSources(runs) {
  const chart = document.getElementById("sourceChart");
  const verdict = document.getElementById("sourceVerdict");
  const table = document.getElementById("sourceTable");
  const legend = document.getElementById("sourceLegend");
  const note = document.getElementById("sessionNote");
  if (!chart || !runs) return;
  const src = runs.sources;
  const rate = importRate(src);
  const chf = (kwh) => money((kwh || 0) * rate);
  if (!src || !src.kwh) {
    chart.innerHTML =
      '<div class="empty-state">No charging recorded yet - this fills up once a car charges.</div>';
    for (const n of [verdict, table, legend, note]) if (n) n.innerHTML = "";
    return;
  }

  const pct = (v, of) => (of ? (100 * v) / of : 0);
  const own = src.pv_kwh + src.battery_kwh;
  if (verdict) {
    verdict.innerHTML =
      `<strong>${fmt(src.kwh, 1)} kWh into the car, ${chf(src.grid_kwh)} paid</strong>: ` +
      `${fmt(pct(src.pv_kwh, src.kwh), 0)}% roof, ` +
      `${fmt(pct(src.battery_kwh, src.kwh), 0)}% battery, ` +
      `${fmt(pct(src.grid_kwh, src.kwh), 0)}% bought. ` +
      `The ${fmt(own, 1)} kWh of your own would have cost ${chf(own)} more.`;
  }
  if (legend) renderLegend(legend, SOURCE_PARTS);

  const rows = chargeRows(runs);
  const width = chartWidth(chart);
  const narrow = width < 560;
  const rowH = 26;
  const padL = chargeView === "session" ? (narrow ? 82 : 96) : narrow ? 76 : 96;
  const padR = narrow ? 104 : 150;
  const padT = 8;
  chart.innerHTML = "";
  const svg = newSvg(chart, width, rows.length * rowH + 16);
  const x = d3
    .scaleLinear()
    .domain([0, d3.max(rows, (d) => d.kwh) * 1.02 || 1])
    .range([padL, width - padR]);
  const span = (v) => x(v) - x(0);

  rows.forEach((d, i) => {
    const y = padT + i * rowH;
    svg
      .append("text")
      .attr("x", padL - 10)
      .attr("y", y + rowH / 2 + 3)
      .attr("text-anchor", "end")
      .attr("font-size", 11)
      .attr("fill", cssVar("--text-secondary"))
      .text(d.label);

    const tip = (event) =>
      showTip(
        tipRows(d.title, [
          ...SOURCE_PARTS.map((part) => ({
            color: part.color,
            label: part.name,
            value: `${fmt(d[part.key], 2)} kWh · ${chf(d[part.key])} (${fmt(
              pct(d[part.key], d.kwh),
              0
            )}%)`,
          })),
          { label: "Total", value: `${fmt(d.kwh, 2)} kWh · ${chf(d.grid)} paid` },
        ]),
        event
      );

    let cursor = padL;
    for (const part of SOURCE_PARTS) {
      const w = span(d[part.key]);
      if (w < 0.6) continue;
      svg
        .append("rect")
        .attr("x", cursor)
        // A 2px surface gap between segments, so the boundary is a gap
        // and not only a change of hue.
        .attr("width", Math.max(1, w - 2))
        .attr("y", y + 5)
        .attr("height", rowH - 12)
        .attr("rx", 3)
        .attr("fill", cssVar(part.color));
      cursor += w;
    }
    // One hit target per row, the whole width: the tooltip lists all three.
    svg
      .append("rect")
      .attr("x", 0)
      .attr("y", y)
      .attr("width", width)
      .attr("height", rowH)
      .attr("fill", "transparent")
      .on("mousemove", tip)
      .on("mouseleave", hideTip);

    svg
      .append("text")
      .attr("x", cursor + 6)
      .attr("y", y + rowH / 2 + 3)
      .attr("font-size", 11)
      .attr("fill", cssVar("--text-secondary"))
      .attr("pointer-events", "none")
      .text(
        narrow
          ? `${fmt(d.kwh, 1)} kWh · ${chf(d.grid)}`
          : `${fmt(d.kwh, 1)} kWh · ${chf(d.grid)} · ${fmt(pct(d.pv + d.battery, d.kwh), 0)}% own`
      );
  });

  if (note) {
    const reconstructed = (runs.sessions || []).length - (runs.measured_count || 0);
    note.textContent =
      `Last ${runs.days} days: ${fmt(runs.total_kwh, 1)} kWh in ${(runs.sessions || []).length} session(s). ` +
      (reconstructed === 0
        ? "All measured by the clamp meter on the charger's feed."
        : (runs.measured_count || 0) === 0
        ? "All reconstructed from the charger's counter, from before the clamp meter went in."
        : `${runs.measured_count} measured by the clamp meter (${fmt(
            runs.measured_kwh,
            1
          )} kWh), ${reconstructed} reconstructed from the charger's counter from before it went in.`);
  }

  if (table) {
    const saving = (src.public_chf_per_kwh || 0) - rate;
    const head = chargeView === "session" ? "Session" : "Day";
    table.innerHTML = `
      <table class="data">
        <thead><tr>
          <th>${head}</th><th>Roof kWh</th><th>Battery kWh</th><th>Grid kWh</th><th>Total kWh</th><th>Paid</th><th>Own</th>
        </tr></thead>
        <tbody>${rows
          .map(
            (d) => `<tr>
              <td>${d.title}</td>
              <td>${fmt(d.pv, 2)}</td>
              <td>${fmt(d.battery, 2)}</td>
              <td>${fmt(d.grid, 2)}</td>
              <td>${fmt(d.kwh, 2)}</td>
              <td>${chf(d.grid)}</td>
              <td>${fmt(pct(d.pv + d.battery, d.kwh), 0)}%</td>
            </tr>`
          )
          .join("")}
          <tr>
            <td><strong>All ${fmt(src.days, 0)} days recorded</strong></td>
            <td><strong>${fmt(src.pv_kwh, 2)}</strong></td>
            <td><strong>${fmt(src.battery_kwh, 2)}</strong></td>
            <td><strong>${fmt(src.grid_kwh, 2)}</strong></td>
            <td><strong>${fmt(src.kwh, 2)}</strong></td>
            <td><strong>${chf(src.grid_kwh)}</strong></td>
            <td><strong>${fmt(pct(own, src.kwh), 0)}%</strong></td>
          </tr>
        </tbody>
      </table>
      <p class="chart-note">
        Priced at the import tariff of ${fmt(rate, 2)} per kWh. Against a
        public charger at ${fmt(src.public_chf_per_kwh, 2)} a self-supplied
        kWh saves ${fmt(src.public_chf_per_kwh, 2)} and a bought one
        ${fmt(saving, 2)}; the energy page's payback splits it that way.
      </p>`;
  }
}

function initChargeView() {
  const row = document.getElementById("chargeView");
  if (!row) return;
  row.addEventListener("click", (evt) => {
    const btn = evt.target.closest("button[data-view]");
    if (!btn) return;
    chargeView = btn.dataset.view;
    for (const b of row.querySelectorAll("button")) b.setAttribute("aria-pressed", String(b === btn));
    if (lastPayloads) renderSources(lastPayloads.runs);
  });
}
initChargeView();

// The car against the one it replaces. Two conversions sit between the
// measurement and the answer - how far a kWh goes, and what a petrol car
// burns - so both are printed next to the result rather than buried in a
// config file.
//
// A line and not twelve bars: the quantity is continuous in the season
// and the shape is the message, a trough in January and a peak in July.
// One series, so no legend - the title names it - and the months that
// are actually in the record carry a dot and their own label, which is
// the direct labelling that keeps the modelled curve and the measured
// months apart without a second colour.
const MONTH_LABELS = "JFMAMJJASOND".split("");

function finDateLabel(iso) {
  const when = new Date(`${iso}T12:00:00`);
  return Number.isNaN(when.getTime())
    ? iso
    : when.toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" });
}

function finMonthLabel(iso) {
  return new Date(`${iso}-01T12:00:00`).toLocaleDateString("en-GB", {
    month: "short",
    year: "numeric",
  });
}

function renderIce(ice) {
  const card = document.getElementById("iceCard");
  if (!card) return;
  if (!ice || !ice.km) {
    card.hidden = true;
    return;
  }
  card.hidden = false;

  const verdict = document.getElementById("iceVerdict");
  if (verdict) {
    verdict.innerHTML =
      `<strong>${fmt(ice.km, 0)} km</strong> out of the charging so far. ` +
      `A petrol car at ${fmt(ice.l_per_100km, 1)} l/100 km would have burnt ` +
      `<strong>${fmt(ice.litres, 1)} litres</strong> for them - ` +
      `${money(ice.fuel_chf)} at ${fmt(ice.fuel_chf_per_litre, 2)} a litre. ` +
      `The electricity that had to be bought for the same kilometres cost ` +
      `${money(ice.power_chf)}, so <strong>${money(ice.saved_chf)} saved</strong>.`;
  }

  // The projection, directly under the measurement it is built from and
  // never without it: twelve months of a fortnight's driving is a big
  // multiplication, and the reader should see what got multiplied.
  const year = document.getElementById("iceYear");
  if (year) {
    const a = ice.annual;
    year.innerHTML = !a
      ? ""
      // Whole francs: centimes on a figure extrapolated from a
      // fortnight would be a claim the arithmetic cannot support.
      : `<strong>Over a year at this rate: ${fmt(a.saved_chf, 0)} ${ice.currency}` +
        (a.saved_low && a.saved_high
          ? `, between ${fmt(a.saved_low, 0)} and ${fmt(a.saved_high, 0)} over the ` +
            `range the pump price has been in`
          : "") +
        `.</strong> ` +
        `${fmt(a.km_per_day, 0)} km a day - the measured ${fmt(
          ice.km,
          0
        )} km over ${fmt(ice.window_days, 1)} days with the wallbox - comes to ` +
        `${fmt(a.km, 0)} km, ${fmt(a.kwh, 0)} kWh and ${fmt(a.litres, 0)} litres of ` +
        `petrol avoided between ${finMonthLabel(a.from)} and ${finMonthLabel(a.to)}. ` +
        `Scaled on kilometres and not on kilowatt-hours, because the same journey ` +
        `needs ${fmt(100 * (ice.curve[0] / ice.curve[6] - 1) * -1, 0)}% less energy in ` +
        `July than in January. The share bought from the grid is held at the ` +
        `${fmt(100 * a.grid_share, 0)}% measured so far; in summer the roof will cover ` +
        `more of it, so this is the low end.`;
  }

  // The curve.
  const chart = document.getElementById("iceChart");
  const width = chartWidth(chart);
  const height = 150;
  const padL = 34;
  const padR = 12;
  const padT = 14;
  const padB = 22;
  chart.innerHTML = "";
  const svg = newSvg(chart, width, height);
  const curve = ice.curve || [];
  const narrow = width < 560;
  // Two labelled months next to each other collide at phone width, where
  // "5.0 km/kWh" is a sixth of the chart. The unit moves to the axis note
  // there, and a label whose neighbour is also labelled goes below the
  // line instead of above it.
  let lastLabelled = -9;
  const byMonth = new Map(
    (ice.by_month || []).map((m) => [parseInt(m.month.slice(5, 7), 10) - 1, m])
  );

  const x = d3
    .scalePoint()
    .domain(d3.range(12))
    .range([padL, width - padR]);
  // The axis does not start at zero, and the note says so: from zero the
  // whole seasonal swing would be four pixels of a bar nobody can read.
  const lo = d3.min(curve) * 0.94;
  const hi = d3.max(curve) * 1.04;
  const y = d3.scaleLinear().domain([lo, hi]).range([height - padB, padT]);

  for (const tick of y.ticks(3)) {
    svg
      .append("line")
      .attr("x1", padL)
      .attr("x2", width - padR)
      .attr("y1", y(tick))
      .attr("y2", y(tick))
      .attr("stroke", cssVar("--gridline"))
      .attr("stroke-width", 1);
    svg
      .append("text")
      .attr("x", padL - 6)
      .attr("y", y(tick) + 3)
      .attr("text-anchor", "end")
      .attr("font-size", 10)
      .attr("fill", cssVar("--text-muted"))
      .text(fmt(tick, 1));
  }

  svg
    .append("path")
    .datum(curve)
    .attr("fill", "none")
    .attr("stroke", cssVar("--series-wallbox"))
    .attr("stroke-width", 2)
    .attr("stroke-linejoin", "round")
    .attr(
      "d",
      d3
        .line()
        .x((d, i) => x(i))
        .y((d) => y(d))
        .curve(d3.curveMonotoneX)
    );

  curve.forEach((value, i) => {
    const month = byMonth.get(i);
    svg
      .append("text")
      .attr("x", x(i))
      .attr("y", height - 6)
      .attr("text-anchor", "middle")
      .attr("font-size", 10)
      .attr("fill", month ? cssVar("--text-primary") : cssVar("--text-muted"))
      .attr("font-weight", month ? 600 : 400)
      .text(MONTH_LABELS[i]);
    if (month) {
      svg
        .append("circle")
        .attr("cx", x(i))
        .attr("cy", y(value))
        .attr("r", 4)
        .attr("fill", cssVar("--series-wallbox"))
        .attr("stroke", cssVar("--surface-1"))
        .attr("stroke-width", 2);
      const below = i - lastLabelled <= 1;
      lastLabelled = i;
      svg
        .append("text")
        .attr("x", x(i))
        .attr("y", y(value) + (below ? 17 : -10))
        .attr("text-anchor", i === 11 ? "end" : "middle")
        .attr("font-size", 10)
        .attr("fill", cssVar("--text-secondary"))
        .text(narrow ? fmt(value, 1) : `${fmt(value, 1)} km/kWh`);
    }
    // A hit target the width of the month, not the width of the dot.
    svg
      .append("rect")
      .attr("x", x(i) - (width - padL - padR) / 24)
      .attr("y", padT)
      .attr("width", (width - padL - padR) / 12)
      .attr("height", height - padT - padB)
      .attr("fill", "transparent")
      .on("mousemove", (event) =>
        showTip(
          tipRows(
            new Date(2026, i, 1).toLocaleDateString("en-GB", { month: "long" }),
            [
              { color: "--series-wallbox", label: "Range", value: `${fmt(value, 2)} km/kWh` },
              month
                ? { label: "Charged", value: `${fmt(month.kwh, 1)} kWh = ${fmt(month.km, 0)} km` }
                : { label: "Charged", value: "nothing recorded" },
            ]
          ),
          event
        )
      )
      .on("mouseleave", hideTip);
  });

  const table = document.getElementById("iceTable");
  if (table) {
    table.innerHTML = `
      <table class="data">
        <thead><tr>
          <th>Month</th><th>km/kWh</th><th>Charged</th><th>Distance</th>
          <th>Petrol</th><th>Petrol cost</th><th>Power bought</th>
        </tr></thead>
        <tbody>${(ice.by_month || [])
          .map(
            (m) => `<tr>
              <td>${new Date(`${m.month}-01T12:00:00`).toLocaleDateString("en-GB", {
                month: "long",
                year: "numeric",
              })}</td>
              <td>${fmt(m.km_per_kwh, 2)}</td>
              <td>${fmt(m.kwh, 1)} kWh</td>
              <td>${fmt(m.km, 0)} km</td>
              <td>${fmt(m.litres, 1)} l</td>
              <td>${money(m.fuel_chf)}</td>
              <td>${money(m.power_chf)}</td>
            </tr>`
          )
          .join("")}
          <tr>
            <td><strong>Total measured</strong></td>
            <td>-</td>
            <td><strong>-</strong></td>
            <td><strong>${fmt(ice.km, 0)} km</strong></td>
            <td><strong>${fmt(ice.litres, 1)} l</strong></td>
            <td><strong>${money(ice.fuel_chf)}</strong></td>
            <td><strong>${money(ice.power_chf)}</strong></td>
          </tr>
        </tbody>
      </table>
      ${iceYearTable(ice)}
      <p class="chart-note">
        The kilowatt-hours are measured; everything after them rests on two
        settings. <code>EV_KM_PER_KWH</code> is ${fmt(ice.km_per_kwh, 1)} km per kWh
        as the car reports it, which counts energy out of the battery;
        the clamp counts energy into the charger, and
        <code>EV_CHARGING_LOSS_PCT</code> of
        ${fmt(ice.charging_loss_pct, 0)}% is the difference. The curve and
        the dots are therefore
        ${fmt(ice.km_per_kwh_at_wall, 2)} km per kWh <em>at the wallbox</em>
        on the year's average, which is the figure the money rests on. The
        month-by-month spread comes from this car's published consumption
        at -10&nbsp;°C and at 23&nbsp;°C read against the local monthly
        mean temperatures: 17% between January and July. It is a floor,
        because a commute happens at the cold end of the day and a short
        winter trip pays for heating the cabin over fewer kilometres.
        <code>ICE_L_PER_100KM</code> is ${fmt(ice.l_per_100km, 1)} l/100 km,
        and it carries no season of its own although a cold engine also
        drinks more - which makes this the conservative comparison.
        <strong>The pump price is a setting and not a feed</strong>:
        ${fmt(ice.fuel_chf_per_litre, 2)} a litre${
          ice.fuel_price_as_of ? `, last checked ${finDateLabel(ice.fuel_price_as_of)}` : ""
        }${
          ice.fuel_price_range
            ? ` against a range of ${fmt(ice.fuel_price_range[0], 2)} to ${fmt(
                ice.fuel_price_range[1],
                2
              )} over the past months`
            : ""
        }. Nothing here fetches it, and nothing could usefully forecast it${
          ice.annual
            ? ` - every 10 rappen on the litre is ${fmt(
                ice.annual.chf_per_10rp,
                0
              )} ${ice.currency} a year`
            : ""
        }. The y-axis does not start at zero. This is not in the payback on the
        energy page and must not be added to it: that one is measured
        against charging the same car at a public charger, and a car cannot
        be compared with two different alternatives at once.
      </p>`;
  }
}

// The twelve months ahead, kept in its own table under the measured
// one so that a projection is never mistaken for a reading.
function iceYearTable(ice) {
  const a = ice.annual;
  if (!a) return "";
  return `
    <p class="chart-note" style="margin-top:14px">
      <strong>Projected over the next twelve months</strong> at
      ${fmt(a.km_per_day, 0)} km a day, each month at its own range and its own
      tariff. Not a measurement.
    </p>
    <table class="data">
      <thead><tr>
        <th>Month</th><th>km/kWh</th><th>Distance</th><th>Energy</th>
        <th>Petrol</th><th>Petrol cost</th><th>Power bought</th><th>Saved</th>
      </tr></thead>
      <tbody>${a.by_month
        .map(
          (m) => `<tr>
            <td>${finMonthLabel(m.month)}</td>
            <td>${fmt(m.km_per_kwh, 2)}</td>
            <td>${fmt(m.km, 0)} km</td>
            <td>${fmt(m.kwh, 0)} kWh</td>
            <td>${fmt(m.litres, 0)} l</td>
            <td>${money(m.fuel_chf)}</td>
            <td>${money(m.power_chf)}</td>
            <td>${money(m.saved_chf)}</td>
          </tr>`
        )
        .join("")}
        <tr>
          <td><strong>Year</strong></td>
          <td>-</td>
          <td><strong>${fmt(a.km, 0)} km</strong></td>
          <td><strong>${fmt(a.kwh, 0)} kWh</strong></td>
          <td><strong>${fmt(a.litres, 0)} l</strong></td>
          <td><strong>${money(a.fuel_chf)}</strong></td>
          <td><strong>${money(a.power_chf)}</strong></td>
          <td><strong>${money(a.saved_chf)}</strong></td>
        </tr>
      </tbody>
    </table>`;
}

// The clamp meter against the charger's own registers. Not a chart: three
// comparisons, each of which needs a sentence about what it does and does
// not establish, which is a table's job and not a bar's.
function renderCrosscheck(x) {
  const body = document.getElementById("crosscheckBody");
  if (!body) return;
  if (!x.metered_samples) {
    body.innerHTML =
      '<div class="empty-state">No clamp meter reading on the charger\'s feed yet.</div>';
    return;
  }

  const rows = [];

  // 1. Standby. The clamp resolves it; the register cannot.
  rows.push({
    what: "Standby, nothing plugged in",
    clamp: x.standby_kw === null ? "-" : `${fmt(x.standby_kw * 1000, 0)} W`,
    charger: "0.0 kW",
    says:
      x.standby_kw === null
        ? "Not yet observed."
        : `Agreed, as far as the charger can tell: ${fmt(
            x.standby_kw * 1000,
            0
          )} W is far below the 100 W step its power register works in, so 0.0 kW is the only answer it can give.`,
  });

  // 2. Power, where both were recorded on the same tick.
  rows.push({
    what: `Power, ${x.power_samples} sample(s) compared`,
    clamp: x.power_mean_diff_kw === null ? "-" : "reference",
    charger:
      x.power_mean_diff_kw === null
        ? "-"
        : `${x.power_mean_diff_kw > 0 ? "+" : ""}${fmt(x.power_mean_diff_kw * 1000, 0)} W mean`,
    says:
      x.power_samples === 0
        ? "The charger has not been read while the clamp was fitted."
        : `Worst single difference ${fmt(
            Math.abs(x.power_max_diff_kw) * 1000,
            0
          )} W. This only ever compares an idle charger - it is read at no other time - so it pins the zero point and nothing about the scale.`,
  });

  // 3. The two counters. The real check, and it needs a charge.
  const m = x.meter_counter;
  const c = x.charger_counter;
  rows.push({
    what: m ? `Energy over ${fmt(m.hours, 1)} h` : "Energy",
    clamp: m ? `${fmt(m.kwh, 3)} kWh` : "-",
    charger: c ? `${fmt(c.kwh, 1)} kWh` : "-",
    says:
      !m || !c
        ? "Needs both counters recorded at two points in time."
        : c.kwh === 0 && m.kwh < x.register_resolution_kwh
        ? `Consistent but uninformative: ${fmt(
            m.kwh * 1000,
            0
          )} Wh is below the charger's own 0.1 kWh resolution, so its counter could not have moved. This is the comparison that will settle the question, and it needs a charge.`
        : `Difference ${fmt(m.kwh - c.kwh, 2)} kWh, ${
            c.kwh ? fmt(((m.kwh - c.kwh) / c.kwh) * 100, 1) : "-"
          }% of the charger's own figure.`,
  });

  // Four columns, one of which is a sentence, is a table where there is
  // room for one and a mess where there is not. On a phone the same rows
  // become a stack.
  const narrow = (body.clientWidth || window.innerWidth) < 560;
  body.innerHTML = narrow
    ? `<div class="xcheck">${rows
        .map(
          (r) => `<div class="row">
            <div class="what">${r.what}</div>
            <div class="values"><span>Clamp ${r.clamp}</span><span>Charger ${r.charger}</span></div>
            <div class="says">${r.says}</div>
          </div>`
        )
        .join("")}</div>`
    : `<table class="data">
      <thead><tr><th>Quantity</th><th>Clamp meter</th><th>Charger</th><th>What that establishes</th></tr></thead>
      <tbody>${rows
        .map(
          (r) => `<tr>
            <td>${r.what}</td>
            <td>${r.clamp}</td>
            <td>${r.charger}</td>
            <td>${r.says}</td>
          </tr>`
        )
        .join("")}</tbody>
    </table>`;
}

function renderEvents(payload) {
  const list = document.getElementById("eventList");
  if (!list) return;
  const events = payload.events || [];
  if (!events.length) {
    list.innerHTML = '<li class="empty-state">No state change recorded yet.</li>';
    return;
  }
  list.innerHTML = events
    .map(
      (e) => `<li>
        <span class="when">${new Date(e.timestamp).toLocaleString("en-GB")}</span>
        <span class="kind">${e.event}</span>
        <span>${e.detail}</span>
      </li>`
    )
    .join("");
}

async function loadSessions() {
  try {
    const [sessions, events, crosscheck] = await Promise.all([
      fetch(`/api/wallbox/sessions?days=${SESSION_DAYS}`).then((r) => r.json()),
      fetch(`/api/wallbox/events?days=${SESSION_DAYS}&limit=50`).then((r) => r.json()),
      fetch(`/api/wallbox/crosscheck?days=${SESSION_DAYS}`).then((r) => r.json()),
    ]);
    lastPayloads = { runs: sessions, compare: crosscheck };
    renderSources(sessions);
    renderIce(sessions.ice);
    renderEvents(events);
    renderCrosscheck(crosscheck);
  } catch (err) {
    document.getElementById("sessionNote").textContent =
      `Wallbox history unavailable (${err.message})`;
  }
}

// The prices come with the live payload, which app.js is already fetching -
// so the first draw waits for it instead of asking the service a third
// time for the same three numbers.
// Charts are sized to their container at draw time, so a rotated phone
// redraws. The payloads are kept rather than refetched.
let lastPayloads = null;
onViewportResize(() => {
  if (!lastPayloads) return;
  renderSources(lastPayloads.runs);
  renderIce(lastPayloads.runs.ice);
  renderCrosscheck(lastPayloads.compare);
});

let sessionsStarted = false;
window.onLiveData = (latest) => {
  setTariff(latest.tariff);
  if (sessionsStarted) return;
  sessionsStarted = true;
  loadSessions();
  window.setInterval(loadSessions, SESSION_REFRESH_MS);
};

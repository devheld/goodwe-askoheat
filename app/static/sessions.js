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

function renderSources(src) {
  const chart = document.getElementById("sourceChart");
  const verdict = document.getElementById("sourceVerdict");
  const table = document.getElementById("sourceTable");
  const legend = document.getElementById("sourceLegend");
  if (!chart) return;
  if (!src || !src.kwh) {
    chart.innerHTML =
      '<div class="empty-state">No charging recorded yet - this fills up once a car charges.</div>';
    if (verdict) verdict.textContent = "";
    if (table) table.innerHTML = "";
    if (legend) legend.innerHTML = "";
    return;
  }

  const pct = (v) => (100 * v) / src.kwh;
  const own = src.pv_kwh + src.battery_kwh;
  if (verdict) {
    verdict.innerHTML =
      `<strong>${fmt(src.kwh, 1)} kWh into the car</strong>: ` +
      `${fmt(pct(src.pv_kwh), 0)}% straight off the roof, ` +
      `${fmt(pct(src.battery_kwh), 0)}% out of the battery, ` +
      `${fmt(pct(src.grid_kwh), 0)}% bought. ` +
      `${fmt(own, 1)} kWh never went through a meter.`;
  }
  if (legend) renderLegend(legend, SOURCE_PARTS);

  // Newest first, same as the session list below it.
  const days = (src.by_day || []).filter((d) => d.kwh > 0.05).slice(-20).reverse();
  const width = chartWidth(chart);
  const narrow = width < 560;
  const rowH = 26;
  const padL = narrow ? 58 : 96;
  const padR = narrow ? 56 : 92;
  const padT = 8;
  chart.innerHTML = "";
  const svg = newSvg(chart, width, days.length * rowH + 16);
  const x = d3
    .scaleLinear()
    .domain([0, d3.max(days, (d) => d.kwh) * 1.05 || 1])
    .range([padL, width - padR]);
  const span = (v) => x(v) - x(0);

  days.forEach((d, i) => {
    const y = padT + i * rowH;
    const when = new Date(`${d.day}T12:00:00`);
    svg
      .append("text")
      .attr("x", padL - 10)
      .attr("y", y + rowH / 2 + 3)
      .attr("text-anchor", "end")
      .attr("font-size", 11)
      .attr("fill", cssVar("--text-secondary"))
      .text(
        when.toLocaleDateString("en-GB", {
          weekday: narrow ? undefined : "short",
          day: "2-digit",
          month: "2-digit",
        })
      );

    let cursor = padL;
    for (const part of SOURCE_PARTS) {
      const w = span(d[part.key] || 0);
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
        .attr("fill", cssVar(part.color))
        .on("mousemove", (event) =>
          showTip(
            tipRows(when.toLocaleDateString("en-GB"), [
              {
                color: part.color,
                label: part.name,
                value: `${fmt(d[part.key], 2)} kWh (${fmt(
                  (100 * d[part.key]) / d.kwh,
                  0
                )}%)`,
              },
              { label: "That day", value: `${fmt(d.kwh, 2)} kWh` },
            ]),
            event
          )
        )
        .on("mouseleave", hideTip);
      cursor += w;
    }

    svg
      .append("text")
      .attr("x", cursor + 6)
      .attr("y", y + rowH / 2 + 3)
      .attr("font-size", 11)
      .attr("fill", cssVar("--text-secondary"))
      .text(
        narrow
          ? `${fmt(d.kwh, 1)} kWh`
          : `${fmt(d.kwh, 1)} kWh · ${fmt(
              (100 * (d.pv + d.battery)) / d.kwh,
              0
            )}% own`
      );
  });

  if (table) {
    const saving = (src.public_chf_per_kwh || 0) - (src.import_chf_per_kwh || 0);
    table.innerHTML = `
      <table class="data">
        <thead><tr>
          <th>Day</th><th>Roof</th><th>Battery</th><th>Grid</th><th>Total</th><th>Self-supplied</th>
        </tr></thead>
        <tbody>${days
          .map(
            (d) => `<tr>
              <td>${new Date(`${d.day}T12:00:00`).toLocaleDateString("en-GB")}</td>
              <td>${fmt(d.pv, 2)}</td>
              <td>${fmt(d.battery, 2)}</td>
              <td>${fmt(d.grid, 2)}</td>
              <td>${fmt(d.kwh, 2)} kWh</td>
              <td>${fmt((100 * (d.pv + d.battery)) / d.kwh, 0)}%</td>
            </tr>`
          )
          .join("")}
          <tr>
            <td><strong>All ${fmt(src.days, 0)} days recorded</strong></td>
            <td><strong>${fmt(src.pv_kwh, 2)}</strong></td>
            <td><strong>${fmt(src.battery_kwh, 2)}</strong></td>
            <td><strong>${fmt(src.grid_kwh, 2)}</strong></td>
            <td><strong>${fmt(src.kwh, 2)} kWh</strong></td>
            <td><strong>${fmt(pct(own), 0)}%</strong></td>
          </tr>
        </tbody>
      </table>
      <p class="chart-note">
        What the mix is worth: a kWh the roof or the battery supplied saves
        the whole ${fmt(src.public_chf_per_kwh, 2)} a public charger would
        have charged for it, a bought one saves
        ${fmt(src.public_chf_per_kwh, 2)} less the
        ${fmt(src.import_chf_per_kwh, 2)} it cost, so
        ${fmt(saving, 2)}. The counter on the energy page already splits it
        exactly that way - the ${fmt(src.import_chf_per_kwh, 2)} of a
        self-supplied kWh is credited to the roof in the solar stream and
        the ${fmt(saving, 2)} on top of it to the charger - which is why
        this mix moves money between the two paybacks but never changes
        the total. Charging at noon is worth more than charging at
        midnight; it is the roof that earns the difference, not the car.
      </p>`;
  }
}

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
            <td><strong>Total</strong></td>
            <td>-</td>
            <td><strong>-</strong></td>
            <td><strong>${fmt(ice.km, 0)} km</strong></td>
            <td><strong>${fmt(ice.litres, 1)} l</strong></td>
            <td><strong>${money(ice.fuel_chf)}</strong></td>
            <td><strong>${money(ice.power_chf)}</strong></td>
          </tr>
        </tbody>
      </table>
      <p class="chart-note">
        The kilowatt-hours are measured; everything after them rests on two
        settings. <code>EV_KM_PER_KWH</code> is ${fmt(ice.km_per_kwh, 1)} km
        per kWh <em>at the wallbox</em> as a yearly average - the car's own
        display counts what reached the battery, which is about a tenth
        more optimistic - and the month-by-month spread comes from this
        car's published consumption at -10&nbsp;°C and at 23&nbsp;°C read
        against the local monthly mean temperatures, 18% between January
        and July. Owner logs for the same car differ by 16% between summer
        and winter tyres, so the model is in the right place; it is still a
        floor, because a commute happens at the cold end of the day and a
        short winter trip pays for heating the cabin over fewer kilometres.
        The curve and the dots are km per kWh.
        <code>ICE_L_PER_100KM</code> is ${fmt(ice.l_per_100km, 1)} l/100 km,
        and it carries no season of its own although a cold engine also
        drinks more - which makes this the conservative comparison. The
        y-axis does not start at zero. This is not in the payback on the
        energy page and must not be added to it: that one is measured
        against charging the same car at a public charger, and a car cannot
        be compared with two different alternatives at once.
      </p>`;
  }
}

function renderSessions(payload) {
  const container = document.getElementById("chartSessions");
  const table = document.getElementById("tableSessions");
  if (!container) return;
  const sessions = (payload.sessions || []).slice(0, 20);
  if (!sessions.length) {
    container.innerHTML =
      '<div class="empty-state">No charging session recorded yet - the page fills up once a car charges.</div>';
    table.innerHTML = "";
    return;
  }

  // Measured sessions wear the clamp's colour, reconstructed ones the
  // charger's own - the same pairing as the two lines in the power chart
  // above, so the page only has to teach it once.
  const barColor = (s) =>
    s.measured ? "--series-wallbox-meter" : "--series-wallbox";

  const rowH = 26;
  const width = chartWidth(container);
  const height = sessions.length * rowH + 30;
  // The date gutter and the value label are both text, so their room is
  // measured in characters rather than in a share of the width. At phone
  // width the full 150px gutter took 43% of the chart and the value label
  // ran off the right edge, so both get shorter instead of smaller.
  const narrow = width < 560;
  const padL = narrow ? 88 : 150;
  const padR = narrow ? 58 : 60;
  const padT = 8;
  const svg = newSvg(container, width, height);

  const x = d3
    .scaleLinear()
    .domain([0, d3.max(sessions, (s) => s.energy_kwh) * 1.1 || 1])
    .range([padL, width - padR]);

  sessions.forEach((s, i) => {
    const y = padT + i * rowH;
    const start = new Date(s.start);
    svg
      .append("text")
      .attr("x", padL - 10)
      .attr("y", y + rowH / 2 + 3)
      .attr("text-anchor", "end")
      .attr("font-size", 11)
      .attr("fill", cssVar("--text-secondary"))
      .text(
        narrow
          ? start.toLocaleString("en-GB", {
              day: "2-digit",
              month: "2-digit",
              hour: "2-digit",
              minute: "2-digit",
            })
          : start.toLocaleString("en-GB", {
              weekday: "short",
              day: "2-digit",
              month: "2-digit",
              hour: "2-digit",
              minute: "2-digit",
            })
      );

    const w = Math.max(2, x(s.energy_kwh) - padL);
    svg
      .append("rect")
      .attr("x", padL)
      .attr("y", y + 4)
      .attr("width", w)
      .attr("height", rowH - 10)
      .attr("rx", 4)
      .attr("fill", cssVar(barColor(s)))
      .on("mousemove", (event) =>
        showTip(
          tipRows(start.toLocaleString("en-GB"), [
            { color: barColor(s), label: "Charged", value: kwhWithCost(s.energy_kwh) },
            { label: "Duration", value: `${fmt(s.duration_min, 0)} min` },
            {
              label: s.measured ? "Peak" : "Peak (derived)",
              value: `${fmt(s.peak_kw, 1)} kW`,
            },
            {
              label: "Self-supplied",
              value:
                s.own_pct === null || s.own_pct === undefined
                  ? "-"
                  : `${fmt(s.own_pct, 0)}% (roof ${fmt(s.pv_kwh, 1)}, battery ${fmt(
                      s.battery_kwh,
                      1
                    )}, grid ${fmt(s.grid_kwh, 1)} kWh)`,
            },
            {
              label: "Source",
              value: s.measured
                ? "Clamp meter on the feed"
                : "Charger's counter, placed by house load",
            },
          ]),
          event
        )
      )
      .on("mouseleave", hideTip);

    svg
      .append("text")
      .attr("x", padL + w + 8)
      .attr("y", y + rowH / 2 + 3)
      .attr("font-size", 11)
      .attr("fill", cssVar("--text-secondary"))
      // The cost is in the tooltip and in the table either way, so the
      // label that does not fit is the one that goes.
      .text(
        narrow
          ? `${fmt(s.energy_kwh, 1)} kWh`
          : `${fmt(s.energy_kwh, 1)} kWh (${cost(s.energy_kwh)})`
      );
  });

  table.innerHTML = `
    <table class="data">
      <thead><tr><th>Start</th><th>Duration</th><th>Energy</th><th>Cost</th><th>Peak</th><th>Self-supplied</th><th>Source</th></tr></thead>
      <tbody>${(payload.sessions || [])
        .map(
          (s) => `<tr>
            <td>${new Date(s.start).toLocaleString("en-GB")}</td>
            <td>${fmt(s.duration_min, 0)} min</td>
            <td>${fmt(s.energy_kwh, 2)} kWh</td>
            <td>${cost(s.energy_kwh)}</td>
            <td>${fmt(s.peak_kw, 1)} kW</td>
            <td>${
              s.own_pct === null || s.own_pct === undefined ? "-" : `${fmt(s.own_pct, 0)}%`
            }</td>
            <td>${s.measured ? "Measured" : "Reconstructed"}</td>
          </tr>`
        )
        .join("")}</tbody>
    </table>`;
  // How much of this is measured and how much reconstructed, in the headline
  // rather than only in the table: a total that mixes the two should say so.
  const reconstructed = payload.sessions.length - (payload.measured_count || 0);
  document.getElementById("sessionNote").textContent =
    `Last ${payload.days} days - ${fmt(payload.total_kwh, 1)} kWh in ${
      payload.sessions.length
    } session(s) worth ${cost(payload.total_kwh)}. ` +
    (reconstructed === 0
      ? "All of it measured by the clamp meter on the charger's feed."
      : (payload.measured_count || 0) === 0
      ? "All of it reconstructed from the charger's counter, from before the clamp meter went in."
      : `${payload.measured_count} measured by the clamp meter (${fmt(
          payload.measured_kwh,
          1
        )} kWh), ${reconstructed} reconstructed from the charger's counter from before it went in.`);
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
    renderSessions(sessions);
    renderSources(sessions.sources);
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
  renderSessions(lastPayloads.runs);
  renderSources(lastPayloads.runs.sources);
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

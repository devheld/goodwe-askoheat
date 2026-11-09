// The second power plant: a 600 W balcony unit whose inverter is plugged
// into a myStrom switch.
//
// The reason this has a page of its own rather than two more lines on the
// energy page is that it is a different machine with a different meter and
// a different owner's-manual worth of caveats. Nothing it makes passes
// through the house inverter, so it is absent from pv_kw, from the roof's
// self-consumption and from the roof's payback - and the one place the two
// plants do meet, the export meter, is where the money has to be divided
// rather than shared. That division is the subject of the second card.
//
// Colours: the production is the project's solar blue, because that is
// what it is, and the part that left the house is the project's export
// green, because that is what every other page calls it. The pair clears
// the palette validator in both modes (CVD delta-E 26.5 light / 27.3 dark,
// normal vision 29.0 / 29.9), which is why no twelfth hue was invented for
// this page.

let balconyEnergy = null;
let balconyLive = null;

function balEl(id) {
  return document.getElementById(id);
}

// ------------------------------------------------------------- the tiles

function renderBalconyTiles() {
  const wrap = balEl("tilesBalcony");
  if (!wrap) return;
  const live = (balconyLive || {}).balcony;
  const block = balconyEnergy && balconyEnergy.balcony;
  const nominal = (balconyLive || {}).balcony_nominal_w;

  const tiles = [];
  const add = (label, value, sub, color) =>
    tiles.push({ label, value, sub: sub || "", color: color || "--series-pv" });

  if (live && live.online) {
    const watts = (live.power_kw || 0) * 1000;
    add(
      "Right now",
      `${fmt(watts, 0)} W`,
      // Against the nameplate rather than on its own: 430 W means one
      // thing under a 600 W rating and another under a 300 W one.
      nominal ? `${fmt((100 * watts) / nominal, 0)}% of ${fmt(nominal, 0)} W` : "",
      "--series-pv"
    );
  } else {
    add(
      "Right now",
      "-",
      live ? "The plug did not answer" : "No plug configured",
      "--baseline"
    );
  }

  if (block) {
    add("Made so far today", kwh(block.kwh, 2), "");
    add(
      "Kept in the house",
      block.self_pct === null ? "-" : `${fmt(block.self_pct, 0)}%`,
      `${fmt(block.export_kwh, 2)} kWh went out through the meter`,
      "--series-grid-export"
    );
    add("Earned today", money(block.chf), "", "--series-pv");
  }

  wrap.innerHTML = tiles
    .map(
      (t) => `
      <div class="tile">
        <div class="label"><span class="dot" style="background:${cssVar(t.color)}"></span>${t.label}</div>
        <div class="value">${t.value}</div>
        ${t.sub ? `<div class="sub">${t.sub}</div>` : ""}
      </div>`
    )
    .join("");

  const note = balEl("balconyNote");
  if (note) {
    const model = (balconyLive || {}).balcony_model;
    note.innerHTML =
      `${model ? `${balEscape(model)}. ` : ""}The plug reports a magnitude and ` +
      `cannot tell which way the current is flowing, so everything here rests ` +
      `on the inverter being what is plugged into it - a heater on the same ` +
      `socket would be booked as production. Energy is integrated from the ` +
      `power readings, not taken from the plug's own counter: that one ` +
      `restarts whenever the plug loses mains, and its per-call field is an ` +
      `average since whoever asked last, which two readers would split ` +
      `between them.`;
  }
}

function balEscape(text) {
  return String(text).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );
}

// ------------------------------------------------------- the power curve

function renderBalconyChart() {
  const wrap = balEl("balconyChartWrap");
  if (!wrap || typeof lastHistory === "undefined") return;
  const { points, hours } = lastHistory;
  // One series, so no legend: the card's title names it. A row recorded
  // before the plug existed carries NULL and is dropped rather than drawn
  // as zero, which would claim a balcony that made nothing.
  const series = [
    {
      name: "Balcony",
      color: "--series-pv",
      points: points.map((p) => ({
        t: new Date(p.timestamp),
        v:
          p.balcony_kw === null || p.balcony_kw === undefined ? null : p.balcony_kw,
      })),
    },
  ];
  const any = series[0].points.some((p) => p.v !== null);
  if (!any) {
    wrap.innerHTML =
      '<div class="empty-state">Nothing recorded from the plug in this range.</div>';
    return;
  }
  renderLineChart(wrap, series, { hours, height: 180, unit: "kW", yMin: 0 });
}

// --------------------------------------------------------- the day split

function renderBalconySplit() {
  const card = balEl("balconySplitCard");
  const host = balEl("balconySplitChart");
  if (!card || !host) return;
  const payload = balconyEnergy;
  const block = payload && payload.balcony;
  const buckets = (payload && payload.buckets) || [];
  const rows = buckets.filter((b) => (b.balcony || 0) > 0.0005);
  if (!block || !rows.length) {
    card.hidden = true;
    return;
  }
  card.hidden = false;

  const parts = [
    { key: "balcony_self", name: "Used in the house", color: "--series-pv" },
    { key: "balcony_export", name: "Exported", color: "--series-grid-export" },
  ];
  renderLegend(balEl("balconyLegend"), parts);

  const width = chartWidth(host);
  const narrow = width < 520;
  const padL = 40;
  const padR = 12;
  const padT = 10;
  const padB = 26;
  const height = 190;
  const svg = newSvg(host, width, height);

  const x = d3
    .scaleBand()
    .domain(rows.map((r) => r.bucket))
    .range([padL, width - padR])
    .padding(0.28);
  const top = d3.max(rows, (r) => r.balcony) || 1;
  const y = d3.scaleLinear().domain([0, top * 1.12]).range([height - padB, padT]);

  for (const tick of y.ticks(4)) {
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

  for (const row of rows) {
    let base = 0;
    for (const part of parts) {
      const value = row[part.key] || 0;
      if (value <= 0) continue;
      const y0 = y(base + value);
      const y1 = y(base);
      // A 2px surface gap between the two segments, so the boundary is a
      // boundary and not a colour change.
      const h = Math.max(1, y1 - y0 - (base > 0 ? 2 : 0));
      svg
        .append("rect")
        .attr("x", x(row.bucket))
        .attr("y", y0)
        .attr("width", x.bandwidth())
        .attr("height", h)
        .attr("rx", 3)
        .attr("fill", cssVar(part.color));
      base += value;
    }
    svg
      .append("rect")
      .attr("x", x(row.bucket))
      .attr("y", padT)
      .attr("width", x.bandwidth())
      .attr("height", height - padT - padB)
      .attr("fill", "transparent")
      .on("mousemove", (event) =>
        showTip(
          tipRows(balDayLabel(row.bucket), [
            {
              color: "--series-pv",
              label: "Used in the house",
              value: kwh(row.balcony_self, 2),
            },
            {
              color: "--series-grid-export",
              label: "Exported",
              value: kwh(row.balcony_export, 2),
            },
            { label: "Made", value: kwh(row.balcony, 2) },
          ]),
          event
        )
      )
      .on("mouseleave", hideTip);
  }

  // Direct labels on the axis, thinned on a narrow screen rather than
  // rotated: a rotated date is slower to read than a missing one.
  const step = narrow ? Math.ceil(rows.length / 5) : Math.ceil(rows.length / 10);
  rows.forEach((row, i) => {
    if (i % step) return;
    svg
      .append("text")
      .attr("x", x(row.bucket) + x.bandwidth() / 2)
      .attr("y", height - 8)
      .attr("text-anchor", "middle")
      .attr("font-size", 10)
      .attr("fill", cssVar("--text-muted"))
      .text(balDayLabel(row.bucket, true));
  });

  const verdict = balEl("balconyVerdict");
  if (verdict) {
    verdict.innerHTML =
      `<strong>${kwh(block.kwh, 1)}</strong> out of the balcony over ` +
      `${fmt(block.hours / 24, 1)} days, and <strong>${fmt(block.self_pct, 0)}% of it ` +
      `stayed in the house</strong> - worth ${money(block.self_chf)} at the buying ` +
      `price against ${money(block.export_chf)} for the ${kwh(block.export_kwh, 1)} ` +
      `that went out. A small plant under a house that rarely draws less than it ` +
      `makes should sit near 100%; anything well below means the roof was already ` +
      `exporting at the time.`;
  }

  renderBalconySplitTable(rows, block);
}

function balDayLabel(bucket, short = false) {
  // Buckets are "%Y-%m-%d" for a day grain and "%Y-%m-%dT%H" for an hour
  // one; both parse once a time is appended.
  const iso = bucket.length === 10 ? `${bucket}T12:00:00` : `${bucket}:00:00`;
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return bucket;
  if (bucket.length > 10) {
    return when.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  }
  return when.toLocaleDateString("en-GB", {
    day: "2-digit",
    month: short ? "2-digit" : "short",
  });
}

function renderBalconySplitTable(rows, block) {
  const host = balEl("balconySplitTable");
  if (!host) return;
  host.innerHTML = `
    <table class="data">
      <thead><tr>
        <th>Day</th><th>Made</th><th>Used in the house</th><th>Exported</th><th>Kept</th>
      </tr></thead>
      <tbody>${rows
        .map(
          (r) => `<tr>
            <td>${balDayLabel(r.bucket)}</td>
            <td>${kwh(r.balcony, 2)}</td>
            <td>${kwh(r.balcony_self, 2)}</td>
            <td>${kwh(r.balcony_export, 2)}</td>
            <td>${r.balcony > 0 ? `${fmt((100 * r.balcony_self) / r.balcony, 0)}%` : "-"}</td>
          </tr>`
        )
        .join("")}
        <tr>
          <td><strong>Total</strong></td>
          <td><strong>${kwh(block.kwh, 2)}</strong></td>
          <td><strong>${kwh(block.self_kwh, 2)}</strong></td>
          <td><strong>${kwh(block.export_kwh, 2)}</strong></td>
          <td><strong>${block.self_pct === null ? "-" : `${fmt(block.self_pct, 0)}%`}</strong></td>
        </tr>
      </tbody>
    </table>`;
}

// ------------------------------------------------------------- the money

function renderBalconyMoney() {
  const card = balEl("balconyMoneyCard");
  const host = balEl("balconyMoneyTable");
  const verdict = balEl("balconyMoney");
  if (!card || !host) return;
  const block = balconyEnergy && balconyEnergy.balcony;
  if (!block) {
    card.hidden = true;
    return;
  }
  card.hidden = false;

  if (verdict) {
    verdict.innerHTML =
      `<strong>${money(block.chf)}</strong> over this period, and none of it is in ` +
      `the roof's figures. The kilowatt-hours it kept in the house are priced at ` +
      `what they replaced, the buying price; the ones it exported at the feed-in ` +
      `price - and those have been taken off the roof's export credit, because the ` +
      `house has one export meter and two plants behind it.`;
  }

  host.innerHTML = `
    <table class="data">
      <thead><tr><th>Where it went</th><th>Energy</th><th>Priced at</th><th>Worth</th></tr></thead>
      <tbody>
        <tr>
          <td><span style="display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:7px;vertical-align:baseline;background:${cssVar("--series-pv")}"></span>Used in the house</td>
          <td>${kwh(block.self_kwh, 2)}</td>
          <td>${fmt(chartTariff.import_per_kwh, 4)}/kWh, the buying price</td>
          <td>${money(block.self_chf)}</td>
        </tr>
        <tr>
          <td><span style="display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:7px;vertical-align:baseline;background:${cssVar("--series-grid-export")}"></span>Exported</td>
          <td>${kwh(block.export_kwh, 2)}</td>
          <td>${fmt(chartTariff.export_per_kwh, 4)}/kWh, the feed-in price</td>
          <td>${money(block.export_chf)}</td>
        </tr>
        <tr>
          <td><strong>Total</strong></td>
          <td><strong>${kwh(block.kwh, 2)}</strong></td>
          <td>-</td>
          <td><strong>${money(block.chf)}</strong></td>
        </tr>
      </tbody>
    </table>
    <p class="chart-note">
      The plug measured for ${fmt(block.hours, 1)} of the
      ${fmt(block.period_hours, 1)} hours in this period; the rest is before it
      was fitted or while it was away, and is reported as unmeasured rather
      than as a balcony that made nothing. There is no payback clock here yet
      because what the unit cost has not been entered
      (<code>BALCONY_INVESTMENT_CHF</code>); the francs are measured either way.
    </p>`;
}

// ---------------------------------------------------------------- wiring

async function fetchBalconyEnergy(period = "month") {
  try {
    const res = await fetch(`/api/energy?period=${period}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    balconyEnergy = await res.json();
    setTariff(balconyEnergy.tariff);
    renderBalconyTiles();
    renderBalconySplit();
    renderBalconyMoney();
  } catch (err) {
    const host = balEl("balconySplitChart");
    if (host) {
      host.innerHTML = `<div class="empty-state">Failed to load (${err.message})</div>`;
    }
  }
}

// app.js owns the live poll and the history fetch; these two hooks are how
// a page's own file gets a look at each without fetching either again.
window.onLiveData = (latest) => {
  balconyLive = latest;
  renderBalconyTiles();
};
window.onHistoryData = () => renderBalconyChart();

onViewportResize(() => {
  renderBalconyChart();
  renderBalconySplit();
});
fetchBalconyEnergy();
setInterval(() => fetchBalconyEnergy(), 300000);

// Energy analysis page. Every chart here reads kWh that the backend
// integrated from the recorded power samples - there is no meter behind the
// AskoHeat or the house load, so the numbers are as good as the poll
// interval and are labelled as such on the page.

const CONSUMERS = [
  { key: "other", name: "Household", color: "--e-house" },
  { key: "asko", name: "AskoHeat", color: "--e-asko" },
  { key: "wallbox", name: "Wallbox", color: "--e-wallbox" },
];

// The three loss buckets, in the order they are stacked and listed. Each is
// measured a different way, which is why the tile names them separately
// instead of printing one efficiency figure: see app/database.py.
const LOSSES = [
  { key: "loss_overhead", name: "Inverter", color: "--e-loss" },
  { key: "loss_battery", name: "Battery", color: "--e-battery" },
  { key: "loss_curtailed", name: "Held back", color: "--e-curtailed" },
];

const PERIOD_LABELS = {
  today: "today",
  "10d": "the last 10 days",
  month: "this month",
  year: "this year",
  all: "all recorded data",
};

const state = {
  period: "today",
  hidden: new Set(),
  energy: null,
  sessions: null,
  // Filled from /api/energy; the prices themselves live in the .env file,
  // so nothing here hardcodes a tariff.
  tariff: { currency: "CHF", import_per_kwh: 0, export_per_kwh: 0 },
};

const tooltip = document.getElementById("tooltip");

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function fmt(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return value.toLocaleString("en-GB", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function kwh(value, digits = 1) {
  return `${fmt(value, digits)} kWh`;
}

function money(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return `${value.toLocaleString("en-GB", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} ${state.tariff.currency}`;
}

// Consumed kWh are priced at the import tariff even when they came off the
// roof: that is the purchase they replaced. Exported kWh are priced at the
// lower feed-in tariff.
function rateFor(kind) {
  return kind === "export" ? state.tariff.export_per_kwh : state.tariff.import_per_kwh;
}

function cost(kwh, kind = "import") {
  return money((kwh || 0) * rateFor(kind));
}

function kwhWithCost(kwh, kind = "import", digits = 2) {
  return `${fmt(kwh, digits)} kWh (${cost(kwh, kind)})`;
}

function showTip(html, event) {
  tooltip.innerHTML = html;
  tooltip.style.opacity = "1";
  tooltip.setAttribute("aria-hidden", "false");
  const pad = 14;
  const rect = tooltip.getBoundingClientRect();
  let x = event.clientX + pad;
  let y = event.clientY + pad;
  if (x + rect.width > window.innerWidth - 8) x = event.clientX - rect.width - pad;
  if (y + rect.height > window.innerHeight - 8) y = event.clientY - rect.height - pad;
  tooltip.style.left = `${Math.max(8, x)}px`;
  tooltip.style.top = `${Math.max(8, y)}px`;
}

function hideTip() {
  tooltip.style.opacity = "0";
  tooltip.setAttribute("aria-hidden", "true");
}

function tipRows(title, rows) {
  const body = rows
    .map(
      (r) =>
        `<div class="tt-row">${
          r.color ? `<span class="swatch" style="background:${cssVar(r.color)}"></span>` : ""
        }<span>${r.label}</span><span class="tt-val">${r.value}</span></div>`
    )
    .join("");
  return `<div class="tt-title">${title}</div>${body}`;
}

// Bucket keys come back as local-time strings the backend built with
// strftime, so they are parsed as local here too - no Date() timezone
// guessing on a string that never carried a zone.
function bucketLabel(bucket, grain, long = false) {
  if (grain === "hour") {
    const hour = bucket.slice(11, 13);
    return long ? `${bucket.slice(8, 10)}.${bucket.slice(5, 7)}. ${hour}:00` : `${hour}:00`;
  }
  if (grain === "day") {
    const [y, m, d] = bucket.split("-");
    return long ? `${d}.${m}.${y}` : `${d}.${m}.`;
  }
  const [y, m] = bucket.split("-");
  const month = new Date(Number(y), Number(m) - 1, 1).toLocaleDateString("en-GB", {
    month: "short",
  });
  return long ? `${month} ${y}` : `${month} ${y.slice(2)}`;
}

// --- shared chart scaffolding ------------------------------------------------

function newSvg(container, width, height) {
  container.innerHTML = "";
  const svg = d3
    .select(container)
    .append("svg")
    .attr("class", "chart")
    .attr("viewBox", `0 0 ${width} ${height}`)
    .attr("preserveAspectRatio", "xMidYMid meet");
  return svg;
}

function drawYGrid(svg, y, left, right, { decimals = 0, zeroLine = false } = {}) {
  const ticks = y.ticks(4);
  const g = svg.append("g");
  for (const v of ticks) {
    const yy = y(v);
    g.append("line")
      .attr("x1", left)
      .attr("x2", right)
      .attr("y1", yy)
      .attr("y2", yy)
      .attr("stroke", v === 0 && zeroLine ? cssVar("--baseline") : cssVar("--gridline"))
      .attr("stroke-width", 1);
    g.append("text")
      .attr("x", left - 8)
      .attr("y", yy + 3)
      .attr("text-anchor", "end")
      .attr("font-size", 10)
      .attr("fill", cssVar("--text-muted"))
      .text(v.toFixed(decimals));
  }
}

function drawXLabels(svg, band, buckets, grain, baselineY) {
  // Thin the labels out rather than rotating them: a month of days at 10px
  // would otherwise collide.
  const every = Math.max(1, Math.ceil(buckets.length / 12));
  svg
    .append("g")
    .selectAll("text")
    .data(buckets.filter((_, i) => i % every === 0))
    .join("text")
    .attr("x", (d) => band(d.bucket) + band.bandwidth() / 2)
    .attr("y", baselineY + 14)
    .attr("text-anchor", "middle")
    .attr("font-size", 10)
    .attr("fill", cssVar("--text-muted"))
    .text((d) => bucketLabel(d.bucket, grain));
}

// Bar with the data-end rounded and the baseline end square, so a stack
// still reads as one column.
function barPath(x, y, w, h, r) {
  const radius = Math.min(r, w / 2, Math.max(0, h));
  if (h <= 0.5) return "";
  return `M${x},${y + h}V${y + radius}a${radius},${radius} 0 0 1 ${radius},${-radius}h${
    w - 2 * radius
  }a${radius},${radius} 0 0 1 ${radius},${radius}V${y + h}Z`;
}

function renderLegend(containerId, items, { toggle = false } = {}) {
  const wrap = document.getElementById(containerId);
  wrap.innerHTML = "";
  for (const item of items) {
    const el = document.createElement("span");
    el.className = "item";
    el.innerHTML = `<span class="swatch" style="background:${cssVar(item.color)}"></span>${item.name}`;
    if (toggle) {
      el.setAttribute("aria-pressed", state.hidden.has(item.key) ? "false" : "true");
      el.title = "Click to hide or show this consumer";
      el.addEventListener("click", () => {
        if (state.hidden.has(item.key)) state.hidden.delete(item.key);
        else state.hidden.add(item.key);
        renderEnergy();
      });
    }
    wrap.appendChild(el);
  }
}

// --- KPI tiles ---------------------------------------------------------------

function renderKpis(energy) {
  const t = energy.totals;
  const m = energy.metrics;
  const c = energy.costs;
  // Six tiles with the same three lines, in the order the energy moves:
  // produced, used, and what crossed the meter either way. Then the three
  // that do not measure energy in kWh-and-francs and are left as they were.
  const tiles = [
    {
      label: "PV production",
      value: kwh(t.pv),
      // What the roof was worth, not what it would cost to buy: the part
      // that stayed in the house replaced a purchase, the part that left
      // earned the (much lower) feed-in rate.
      money: money(c.saved),
      sub: `${fmt(c.self_consumed_kwh)} kWh used, ${fmt(t.grid_export)} kWh fed in`,
      color: "--e-solar",
    },
    {
      label: "Total consumption",
      value: kwh(t.consumption),
      money: money(c.consumption_value),
      sub: `${fmt(t.grid_import)} kWh of it from the grid`,
      color: "--e-house",
    },
    {
      label: "Grid import",
      value: kwh(t.grid_import),
      money: money(c.spent),
      sub: `Bought at ${money(state.tariff.import_per_kwh)}/kWh`,
      color: "--e-import",
    },
    {
      label: "Grid feed-in",
      value: kwh(t.grid_export),
      money: money(c.earned),
      sub: `Fed in at ${money(state.tariff.export_per_kwh)}/kWh`,
      color: "--e-export",
    },
    {
      label: "AskoHeat",
      value: kwh(t.asko),
      money: cost(t.asko),
      sub: "Immersion heater",
      color: "--e-asko",
    },
    {
      label: "Wallbox",
      value: kwh(t.wallbox),
      money: cost(t.wallbox),
      sub: "Car charging",
      color: "--e-wallbox",
    },
    {
      label: "Self-sufficiency",
      value: m.autarky_pct === null ? "-" : `${fmt(m.autarky_pct, 0)} %`,
      sub: "Share not bought from the grid",
      color: "--e-solar",
    },
    {
      label: "Battery",
      value: kwh(t.battery_charge),
      sub: `charged, ${fmt(t.battery_discharge)} kWh drawn`,
      color: "--e-battery",
    },
    {
      label: "Losses",
      value: `${fmt(energy.losses.real_kwh)} kWh`,
      sub: lossBreakdown(energy.losses),
      color: "--e-loss",
      wide: true,
    },
  ];
  const wrap = document.getElementById("kpis");
  wrap.innerHTML = "";
  for (const tile of tiles) {
    const el = document.createElement("div");
    el.className = tile.wide ? "tile wide" : "tile";
    el.innerHTML = `
      <div class="label"><span class="dot" style="background:${cssVar(tile.color)}"></span>${tile.label}</div>
      <div class="value">${tile.value}</div>
      ${tile.money ? `<div class="money">${tile.money}</div>` : ""}
      <div class="sub">${tile.sub}</div>`;
    wrap.appendChild(el);
  }
}

// The tile's own breakdown. Each line says which bucket it is, because the
// three are not comparable: the first two are energy that was measured going
// in and never came out, the third is production that never happened and is
// therefore NOT part of the headline figure above it.
function lossBreakdown(L) {
  const head =
    L.real_pct_of_pv === null
      ? ""
      : `<div>${fmt(L.real_pct_of_pv)}% of solar &middot; ${money(L.real_chf)}</div>`;
  const lines = [
    `<div class="line"><span>Inverter standby</span>
       <span class="n">${fmt(L.overhead_kwh)} kWh</span></div>`,
  ];
  if (L.battery_kwh === null) {
    // Most of what went in is still in there - see BATTERY_LOSS_MAX_STORED_SHARE.
    lines.push(`<div class="line"><span>Battery</span>
       <span class="n">period too short</span></div>`);
  } else {
    lines.push(`<div class="line"><span>Battery round trip</span>
       <span class="n">${fmt(L.battery_kwh)} kWh</span></div>`);
  }
  // Listed with the other two but deliberately outside the headline above:
  // this is production that never happened, so adding it to energy that was
  // measured going in and never coming out would be double counting.
  lines.push(`<div class="line"><span>Held back by the plant</span>
       <span class="n">&ge; ${fmt(L.curtailed_kwh)} kWh</span></div>`);
  return head + `<div class="lines">${lines.join("")}</div>`;
}

// --- losses per bucket -------------------------------------------------------

function renderLosses(energy) {
  const container = document.getElementById("chartLoss");
  const buckets = energy.buckets;
  const L = energy.losses;
  const note = document.getElementById("lossNote");
  const rt =
    L.battery_round_trip_pct === null
      ? "the period is too short to separate a round-trip loss from energy still stored"
      : `the battery returned ${fmt(L.battery_round_trip_pct)}% of what went into it`;
  note.textContent =
    `Stacked kWh per bucket. Inverter standby is the gap in the inverter's own ` +
    `balance, ${rt}, and "held back" is production the plant throttled away - ` +
    `a lower bound measured against its own best output in the same ` +
    `${L.curtailment_window_min} minutes, so it is marked with a "greater than".`;

  const total = L.real_kwh + L.curtailed_kwh;
  if (!buckets.length || total <= 0) {
    container.innerHTML = '<div class="empty-state">No losses measured in this period.</div>';
    document.getElementById("curtailNote").textContent = "";
    return;
  }

  const width = 960;
  const height = 240;
  const padL = 48, padR = 12, padT = 12, padB = 28;
  const svg = newSvg(container, width, height);
  const band = d3
    .scaleBand()
    .domain(buckets.map((d) => d.bucket))
    .range([padL, width - padR])
    .padding(0.22);
  const stackMax = d3.max(buckets, (d) => d3.sum(LOSSES, (l) => d[l.key] || 0)) || 0;
  const y = d3
    .scaleLinear()
    .domain([0, stackMax * 1.1 || 1])
    .range([height - padB, padT]);
  drawYGrid(svg, y, padL, width - padR, { decimals: y.domain()[1] < 5 ? 1 : 0 });

  const GAP = 2;
  const layer = svg.append("g");
  for (const bucket of buckets) {
    let acc = 0;
    const segments = LOSSES.map((l) => {
      const value = bucket[l.key] || 0;
      const seg = { l, value, y0: acc, y1: acc + value };
      acc += value;
      return seg;
    }).filter((s) => s.value > 0);
    const top = segments[segments.length - 1];
    for (const seg of segments) {
      const yTop = y(seg.y1);
      const h = Math.max(0, y(seg.y0) - yTop - (seg === top ? 0 : GAP));
      layer
        .append("path")
        .attr(
          "d",
          seg === top
            ? barPath(band(bucket.bucket), yTop, band.bandwidth(), h, 4)
            : `M${band(bucket.bucket)},${yTop}h${band.bandwidth()}v${h}h${-band.bandwidth()}Z`
        )
        .attr("fill", cssVar(seg.l.color));
    }
  }
  drawXLabels(svg, band, buckets, energy.grain, height - padB);

  svg
    .append("g")
    .selectAll("rect")
    .data(buckets)
    .join("rect")
    .attr("x", (d) => band(d.bucket))
    .attr("y", padT)
    .attr("width", band.bandwidth())
    .attr("height", height - padB - padT)
    .attr("fill", "transparent")
    .on("mousemove", (event, d) => {
      const rows = LOSSES.map((l) => ({
        color: l.color,
        label: l.name,
        value: kwhWithCost(d[l.key] || 0),
      }));
      rows.push({ label: "Solar that bucket", value: `${fmt(d.pv, 2)} kWh` });
      showTip(tipRows(bucketLabel(d.bucket, energy.grain, true), rows), event);
    })
    .on("mouseleave", hideTip);

  // Who could have taken the energy the plant held back. This is the only
  // number on the page that grades the control loop rather than the hardware:
  // "nobody asked for it" is the heater sitting at step 0 with a cold tank,
  // which is the one case the loop could have prevented and did not.
  const s = L.curtailed_split;
  const parts = [
    `${fmt(s.tank_hot)} kWh with the tank already too hot to start heating`,
    `${fmt(s.at_max)} kWh with the heater at its last step`,
    `${fmt(s.headroom)} kWh while the heater still had steps left (${money(L.headroom_chf)})`,
    `${fmt(s.missed)} kWh with a cold tank and the heater off`,
  ];
  document.getElementById("curtailNote").textContent =
    `Of the ${fmt(L.curtailed_kwh)} kWh held back: ` + parts.join(", ") + ".";
}

// --- stacked consumption + solar line ----------------------------------------

function renderStack(energy) {
  const container = document.getElementById("chartStack");
  const buckets = energy.buckets;
  if (!buckets.length) {
    container.innerHTML = '<div class="empty-state">Nothing recorded in this period yet.</div>';
    document.getElementById("tableStack").innerHTML = "";
    return;
  }

  const visible = CONSUMERS.filter((c) => !state.hidden.has(c.key));
  const width = 960;
  const height = 300;
  const padL = 48, padR = 12, padT = 12, padB = 28;

  const svg = newSvg(container, width, height);
  const band = d3
    .scaleBand()
    .domain(buckets.map((d) => d.bucket))
    .range([padL, width - padR])
    .padding(0.22);

  const stackMax = d3.max(buckets, (d) => d3.sum(visible, (c) => d[c.key])) || 0;
  const pvMax = d3.max(buckets, (d) => d.pv) || 0;
  const y = d3
    .scaleLinear()
    .domain([0, Math.max(stackMax, pvMax) * 1.1 || 1])
    .range([height - padB, padT]);

  const decimals = y.domain()[1] < 5 ? 1 : 0;
  drawYGrid(svg, y, padL, width - padR, { decimals });

  // Segments are drawn from the baseline up, each one 2px short of the next
  // so the surface shows through and the boundary is visible without a
  // stroke that would tint the colour.
  const GAP = 2;
  const layer = svg.append("g");
  for (const bucket of buckets) {
    let acc = 0;
    const segments = visible.map((c) => {
      const value = bucket[c.key] || 0;
      const seg = { c, value, y0: acc, y1: acc + value };
      acc += value;
      return seg;
    });
    const top = segments[segments.length - 1];
    for (const seg of segments) {
      if (seg.value <= 0) continue;
      const yTop = y(seg.y1);
      const yBottom = y(seg.y0);
      const h = Math.max(0, yBottom - yTop - (seg === top ? 0 : GAP));
      const isTop = seg === top;
      layer
        .append("path")
        .attr(
          "d",
          isTop
            ? barPath(band(bucket.bucket), yTop, band.bandwidth(), h, 4)
            : `M${band(bucket.bucket)},${yTop}h${band.bandwidth()}v${h}h${-band.bandwidth()}Z`
        )
        .attr("fill", cssVar(seg.c.color));
    }
  }

  // Solar on the same kWh axis - never a second y scale.
  const line = d3
    .line()
    .x((d) => band(d.bucket) + band.bandwidth() / 2)
    .y((d) => y(d.pv))
    .curve(d3.curveMonotoneX);
  svg
    .append("path")
    .datum(buckets)
    .attr("fill", "none")
    .attr("stroke", cssVar("--e-solar"))
    .attr("stroke-width", 2)
    .attr("stroke-linejoin", "round")
    .attr("d", line);

  drawXLabels(svg, band, buckets, energy.grain, height - padB);

  // One hover target per bucket, full height so the pointer does not have to
  // find a thin segment.
  svg
    .append("g")
    .selectAll("rect")
    .data(buckets)
    .join("rect")
    .attr("x", (d) => band(d.bucket))
    .attr("y", padT)
    .attr("width", band.bandwidth())
    .attr("height", height - padB - padT)
    .attr("fill", "transparent")
    .on("mousemove", (event, d) => {
      const rows = visible.map((c) => ({
        color: c.color,
        label: c.name,
        value: kwhWithCost(d[c.key]),
      }));
      rows.push({ color: "--e-solar", label: "Solar", value: `${fmt(d.pv, 2)} kWh` });
      rows.push({ label: "Total used", value: kwhWithCost(d.consumption) });
      showTip(tipRows(bucketLabel(d.bucket, energy.grain, true), rows), event);
    })
    .on("mouseleave", hideTip);

  renderStackTable(energy);
}

function renderStackTable(energy) {
  const rows = energy.buckets
    .map(
      (d) => `<tr>
        <td>${bucketLabel(d.bucket, energy.grain, true)}</td>
        <td>${fmt(d.other, 2)}</td>
        <td>${fmt(d.asko, 2)}</td>
        <td>${fmt(d.wallbox, 2)}</td>
        <td>${fmt(d.consumption, 2)}</td>
        <td>${cost(d.consumption)}</td>
        <td>${fmt(d.pv, 2)}</td>
      </tr>`
    )
    .join("");
  document.getElementById("tableStack").innerHTML = `
    <table class="data">
      <thead><tr>
        <th>Bucket</th><th>Household</th><th>AskoHeat</th><th>Wallbox</th>
        <th>Total</th><th>Value</th><th>Solar</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// --- grid balance ------------------------------------------------------------

function renderGrid(energy) {
  const container = document.getElementById("chartGrid");
  const buckets = energy.buckets;
  if (!buckets.length) {
    container.innerHTML = '<div class="empty-state">No grid data in this period.</div>';
    return;
  }

  const width = 960;
  const height = 220;
  const padL = 48, padR = 12, padT = 12, padB = 28;
  const svg = newSvg(container, width, height);

  const band = d3
    .scaleBand()
    .domain(buckets.map((d) => d.bucket))
    .range([padL, width - padR])
    .padding(0.22);

  // Both directions count upwards. A signed axis is compact, but it asks the
  // reader to remember which sign means which direction - and that
  // convention differs between inverter vendors, so the label does the work
  // instead.
  const peak = d3.max(buckets, (d) => Math.max(d.grid_import, d.grid_export)) || 0;
  const y = d3
    .scaleLinear()
    .domain([0, Math.max(peak, 0.1) * 1.1])
    .range([height - padB, padT]);

  drawYGrid(svg, y, padL, width - padR, { decimals: peak < 5 ? 1 : 0 });

  const DIRECTIONS = [
    { key: "grid_import", color: "--e-import" },
    { key: "grid_export", color: "--e-export" },
  ];
  const inner = d3
    .scaleBand()
    .domain(DIRECTIONS.map((d) => d.key))
    .range([0, band.bandwidth()])
    .padding(0.16);

  const zero = y(0);
  const g = svg.append("g");
  for (const d of buckets) {
    for (const dir of DIRECTIONS) {
      const value = d[dir.key];
      if (value <= 0) continue;
      const top = y(value);
      g.append("path")
        .attr(
          "d",
          barPath(band(d.bucket) + inner(dir.key), top, inner.bandwidth(), zero - top, 4)
        )
        .attr("fill", cssVar(dir.color));
    }
  }

  drawXLabels(svg, band, buckets, energy.grain, height - padB);

  svg
    .append("g")
    .selectAll("rect")
    .data(buckets)
    .join("rect")
    .attr("x", (d) => band(d.bucket))
    .attr("y", padT)
    .attr("width", band.bandwidth())
    .attr("height", height - padB - padT)
    .attr("fill", "transparent")
    .on("mousemove", (event, d) => {
      showTip(
        tipRows(bucketLabel(d.bucket, energy.grain, true), [
          { color: "--e-import", label: "Grid import", value: kwhWithCost(d.grid_import) },
          {
            color: "--e-export",
            label: "Grid feed-in",
            value: kwhWithCost(d.grid_export, "export"),
          },
        ]),
        event
      );
    })
    .on("mouseleave", hideTip);
}

// --- donut -------------------------------------------------------------------

function renderDonut(energy) {
  const container = document.getElementById("chartDonut");
  const data = CONSUMERS.map((c) => ({ ...c, value: energy.totals[c.key] })).filter(
    (d) => d.value > 0
  );
  const total = d3.sum(data, (d) => d.value);
  if (!total) {
    container.innerHTML = '<div class="empty-state">No consumption recorded in this period.</div>';
    return;
  }

  const width = 960;
  const height = 240;
  const radius = Math.min(height, 420) / 2 - 10;
  const svg = newSvg(container, width, height);
  const g = svg.append("g").attr("transform", `translate(${width / 2}, ${height / 2})`);

  const pie = d3.pie().value((d) => d.value).sort(null).padAngle(0.012);
  const arc = d3.arc().innerRadius(radius * 0.62).outerRadius(radius).cornerRadius(3);

  g.selectAll("path")
    .data(pie(data))
    .join("path")
    .attr("d", arc)
    .attr("fill", (d) => cssVar(d.data.color))
    .attr("stroke", cssVar("--surface-1"))
    .attr("stroke-width", 2)
    .on("mousemove", (event, d) => {
      showTip(
        tipRows(d.data.name, [
          { color: d.data.color, label: "Energy", value: kwhWithCost(d.data.value) },
          { label: "Share", value: `${fmt((100 * d.data.value) / total, 1)} %` },
        ]),
        event
      );
    })
    .on("mouseleave", hideTip);

  g.append("text")
    .attr("text-anchor", "middle")
    .attr("y", -4)
    .attr("font-size", 24)
    .attr("font-weight", 600)
    .attr("fill", cssVar("--text-primary"))
    .text(`${fmt(total)} kWh`);
  g.append("text")
    .attr("text-anchor", "middle")
    .attr("y", 16)
    .attr("font-size", 12)
    .attr("fill", cssVar("--text-secondary"))
    .text("total consumption");

  // Direct labels: with three slices there is room, so identity never rests
  // on colour alone.
  const labelArc = d3.arc().innerRadius(radius + 14).outerRadius(radius + 14);
  g.selectAll("text.slice")
    .data(pie(data))
    .join("text")
    .attr("class", "slice")
    .attr("transform", (d) => `translate(${labelArc.centroid(d)})`)
    .attr("text-anchor", (d) => ((d.startAngle + d.endAngle) / 2 > Math.PI ? "end" : "start"))
    .attr("font-size", 11)
    .attr("fill", cssVar("--text-secondary"))
    .text((d) => `${d.data.name} ${fmt((100 * d.data.value) / total, 0)}%`);
}

// --- wallbox sessions --------------------------------------------------------

function renderSessions(payload) {
  const container = document.getElementById("chartSessions");
  const table = document.getElementById("tableSessions");
  const sessions = (payload.sessions || []).slice(0, 20);
  if (!sessions.length) {
    container.innerHTML =
      '<div class="empty-state">No charging session recorded yet - the page fills up once a car charges.</div>';
    table.innerHTML = "";
    return;
  }

  const rowH = 26;
  const width = 960;
  const height = sessions.length * rowH + 30;
  const padL = 150, padR = 60, padT = 8;
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
      .text(start.toLocaleString("en-GB", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }));

    const w = Math.max(2, x(s.energy_kwh) - padL);
    svg
      .append("rect")
      .attr("x", padL)
      .attr("y", y + 4)
      .attr("width", w)
      .attr("height", rowH - 10)
      .attr("rx", 4)
      .attr("fill", cssVar("--e-wallbox"))
      .on("mousemove", (event) =>
        showTip(
          tipRows(start.toLocaleString("en-GB"), [
            { color: "--e-wallbox", label: "Charged", value: kwhWithCost(s.energy_kwh) },
            { label: "Duration", value: `${fmt(s.duration_min, 0)} min` },
            { label: "Peak", value: `${fmt(s.peak_kw, 1)} kW` },
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
      .text(`${fmt(s.energy_kwh, 1)} kWh (${cost(s.energy_kwh)})`);
  });

  table.innerHTML = `
    <table class="data">
      <thead><tr><th>Start</th><th>Duration</th><th>Energy</th><th>Cost</th><th>Peak</th></tr></thead>
      <tbody>${(payload.sessions || [])
        .map(
          (s) => `<tr>
            <td>${new Date(s.start).toLocaleString("en-GB")}</td>
            <td>${fmt(s.duration_min, 0)} min</td>
            <td>${fmt(s.energy_kwh, 2)} kWh</td>
            <td>${cost(s.energy_kwh)}</td>
            <td>${fmt(s.peak_kw, 1)} kW</td>
          </tr>`
        )
        .join("")}</tbody>
    </table>`;
  document.getElementById("sessionNote").textContent =
    `Last ${payload.days} days, rebuilt from the recorded samples - ${fmt(payload.total_kwh, 1)} kWh in ${
      payload.sessions.length
    } session(s) worth ${cost(payload.total_kwh)}.`;
}

function renderEvents(payload) {
  const list = document.getElementById("eventList");
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

// --- wiring ------------------------------------------------------------------

function renderEnergy() {
  if (!state.energy) return;
  renderKpis(state.energy);
  // The three stacked consumers can be toggled; solar is the reference line
  // and stays put, so it is listed but not clickable.
  renderLegend("legendStack", CONSUMERS, { toggle: true });
  const solar = document.createElement("span");
  solar.className = "item";
  solar.innerHTML = `<span class="swatch" style="background:${cssVar("--e-solar")}"></span>Solar (line)`;
  document.getElementById("legendStack").appendChild(solar);

  renderLegend("legendGrid", [
    { key: "grid_import", name: "Grid import", color: "--e-import" },
    { key: "grid_export", name: "Grid feed-in", color: "--e-export" },
  ]);
  renderLegend("legendDonut", CONSUMERS);

  renderLegend("legendLoss", LOSSES);

  renderStack(state.energy);
  renderGrid(state.energy);
  renderDonut(state.energy);
  renderLosses(state.energy);
}

async function load() {
  const subtitle = document.getElementById("subtitle");
  try {
    const res = await fetch(`/api/energy?period=${state.period}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.energy = await res.json();
    state.tariff = state.energy.tariff;
    subtitle.textContent = `Energy for ${PERIOD_LABELS[state.period]} - ${fmt(
      state.energy.totals.hours,
      1
    )} hours of samples.`;
    renderEnergy();
  } catch (err) {
    subtitle.textContent = `No energy data (${err.message})`;
  }

  try {
    const [sessions, events] = await Promise.all([
      fetch("/api/wallbox/sessions?days=30").then((r) => r.json()),
      fetch("/api/wallbox/events?days=30&limit=50").then((r) => r.json()),
    ]);
    state.sessions = sessions;
    renderSessions(sessions);
    renderEvents(events);
  } catch (err) {
    document.getElementById("sessionNote").textContent = `Wallbox history unavailable (${err.message})`;
  }

  document.getElementById("footerNote").textContent =
    "kWh are integrated from one power sample per poll interval, not read from a meter. " +
    "Gaps longer than 5 minutes count as 5 minutes, so a service outage cannot invent energy. " +
    `Prices in brackets use ${money(state.tariff.import_per_kwh)}/kWh for consumption and ` +
    `${money(state.tariff.export_per_kwh)}/kWh for feed-in, both set in the .env file.`;
}

document.getElementById("periodRow").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-period]");
  if (!button) return;
  state.period = button.dataset.period;
  for (const b of document.querySelectorAll("#periodRow button")) {
    b.setAttribute("aria-pressed", String(b === button));
  }
  load();
});

window.addEventListener("resize", () => renderEnergy());
load();

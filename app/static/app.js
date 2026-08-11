const TILE_DEFS = [
  { key: "pv_kw", label: "PV Production", unit: "kW", color: "--series-pv" },
  { key: "consumption_kw", label: "Consumption", unit: "kW", color: "--series-cons" },
  { key: "battery_charge_kw", label: "Battery (Charging)", unit: "kW", color: "--series-batt-charge" },
  { key: "battery_discharge_kw", label: "Battery (Discharging)", unit: "kW", color: "--series-batt-discharge" },
  { key: "grid_kw", label: "Grid", unit: "kW", color: "--series-grid" },
];

const ASKO_TILE_DEFS = [
  { key: "step", label: "AskoHeat Step (Target)", unit: "/7", color: "--series-step" },
  { key: "heater_load_w", label: "AskoHeat Load (Actual)", unit: "W", color: "--series-load" },
  { key: "askoheat_temp_c", label: "AskoHeat Temperature", unit: "°C", color: "--series-temp" },
];

// AskoHeat's own temperature limit (its internal thermostat cuts power to
// the heating elements regardless of the commanded step) - shown as a
// reference line on the temperature chart to explain the "step high, load
// 0W" effect.
const ASKOHEAT_MAX_TEMP_C = 65;

const POWER_SERIES_DEFS = [
  { key: "pv_kw", name: "PV", color: "--series-pv" },
  { key: "consumption_kw", name: "Consumption", color: "--series-cons" },
  { key: "battery_charge_kw", name: "Battery Charge", color: "--series-batt-charge" },
  { key: "battery_discharge_kw", name: "Battery Discharge", color: "--series-batt-discharge" },
  { key: "grid_kw", name: "Grid", color: "--series-grid" },
];

let currentHours = 24;

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function fmt(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return value.toFixed(digits);
}

function renderTiles(containerId, defs, latest) {
  const wrap = document.getElementById(containerId);
  wrap.innerHTML = "";
  for (const def of defs) {
    const tile = document.createElement("div");
    tile.className = "tile";
    let valueText;
    let sub = "";
    if (
      def.key === "pv_kw" ||
      def.key === "consumption_kw" ||
      def.key === "battery_charge_kw" ||
      def.key === "battery_discharge_kw" ||
      def.key === "grid_kw"
    ) {
      valueText = `${fmt(latest[def.key])} ${def.unit}`;
    } else if (def.key === "step") {
      valueText = `${latest.step}${def.unit}`;
    } else if (def.key === "heater_load_w") {
      valueText = `${latest.heater_load_w} ${def.unit}`;
    } else if (def.key === "askoheat_temp_c") {
      valueText = latest.askoheat_temp_c === null || latest.askoheat_temp_c === undefined
        ? "-"
        : `${fmt(latest.askoheat_temp_c, 1)} ${def.unit}`;
    }
    tile.innerHTML = `
      <div class="label"><span class="dot" style="background:${cssVar(def.color)}"></span>${def.label}</div>
      <div class="value">${valueText}</div>
      ${sub ? `<div class="sub">${sub}</div>` : ""}
    `;
    wrap.appendChild(tile);
  }
}

async function fetchLive() {
  try {
    const res = await fetch("/api/live");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const latest = await res.json();
    renderTiles("tiles", TILE_DEFS, latest);
    renderTiles("tilesAsko", ASKO_TILE_DEFS, latest);
    document.getElementById("statusLine").textContent =
      latest.battery_charging === undefined ? "" : "Live data";
    document.getElementById("lastUpdate").textContent = new Date(latest.timestamp).toLocaleString("en-GB");
    if (latest.poll_interval_s !== undefined) {
      document.getElementById("updateInterval").textContent = latest.poll_interval_s;
    }
  } catch (err) {
    document.getElementById("statusLine").textContent = "No live data available (" + err.message + ")";
  }
}

function formatTick(date, hours) {
  if (hours <= 24) {
    return date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  }
  return date.toLocaleDateString("en-GB", { day: "2-digit", month: "2-digit" });
}

// Renders a multi-series line chart as SVG with a hover crosshair.
// series: [{ name, color, points: [{t: Date, v: number}] }]
function renderLineChart(container, series, opts) {
  const {
    width = 960,
    height = 220,
    hours,
    stepped = false,
    unit = "",
    yMin: yMinOpt,
    yMax: yMaxOpt,
    yTickStep,
    yDecimals = 1,
    refLine,
  } = opts;
  const padL = 44, padR = 12, padT = 12, padB = 24;
  const innerW = width - padL - padR;
  const innerH = height - padT - padB;

  const allPoints = series.flatMap((s) => s.points);
  if (allPoints.length === 0) {
    container.innerHTML = '<div class="empty-state">No history data for this time range yet.</div>';
    return;
  }

  const tMin = Math.min(...allPoints.map((p) => p.t.getTime()));
  const tMax = Math.max(...allPoints.map((p) => p.t.getTime()));

  // Y domain: either fixed (e.g. heater step 0-7) or derived from the data
  // with some padding.
  let vMin, vMax;
  if (yMinOpt !== undefined && yMaxOpt !== undefined) {
    // both bounds fixed (e.g. heater step 0-7, temperature 0-90) - no
    // extra padding.
    vMin = yMinOpt;
    vMax = yMaxOpt;
  } else {
    // one or no bound fixed - derive the other one(s) from the data and
    // only pad those.
    vMin = yMinOpt !== undefined ? yMinOpt : Math.min(0, ...allPoints.map((p) => p.v));
    vMax = yMaxOpt !== undefined ? yMaxOpt : Math.max(0, ...allPoints.map((p) => p.v));
    if (vMin === vMax) { vMax += 1; }
    const vPad = (vMax - vMin) * 0.1;
    if (yMinOpt === undefined) vMin -= vPad;
    if (yMaxOpt === undefined) vMax += vPad;
  }

  const x = (t) => padL + ((t - tMin) / Math.max(1, tMax - tMin)) * innerW;
  const y = (v) => padT + innerH - ((v - vMin) / (vMax - vMin)) * innerH;

  const gridlineColor = cssVar("--gridline");
  const baselineColor = cssVar("--baseline");
  const mutedColor = cssVar("--text-muted");

  let gridSvg = "";
  if (yTickStep) {
    // Fixed integer steps (e.g. heater step 0,1,2,...,7).
    for (let v = yMinOpt; v <= yMaxOpt + 1e-9; v += yTickStep) {
      const yy = y(v);
      gridSvg += `<line x1="${padL}" x2="${width - padR}" y1="${yy}" y2="${yy}" stroke="${gridlineColor}" stroke-width="1" />`;
      gridSvg += `<text x="${padL - 8}" y="${yy + 3}" text-anchor="end" font-size="10" fill="${mutedColor}">${v.toFixed(yDecimals)}</text>`;
    }
  } else {
    const yTicks = 4;
    for (let i = 0; i <= yTicks; i++) {
      const v = vMin + ((vMax - vMin) * i) / yTicks;
      const yy = y(v);
      gridSvg += `<line x1="${padL}" x2="${width - padR}" y1="${yy}" y2="${yy}" stroke="${gridlineColor}" stroke-width="1" />`;
      gridSvg += `<text x="${padL - 8}" y="${yy + 3}" text-anchor="end" font-size="10" fill="${mutedColor}">${v.toFixed(yDecimals)}</text>`;
    }
  }
  if (vMin <= 0 && vMax >= 0) {
    const zeroY = y(0);
    gridSvg += `<line x1="${padL}" x2="${width - padR}" y1="${zeroY}" y2="${zeroY}" stroke="${baselineColor}" stroke-width="1" />`;
  }

  let refLineSvg = "";
  if (refLine && refLine.value >= vMin && refLine.value <= vMax) {
    const ry = y(refLine.value);
    refLineSvg = `<line x1="${padL}" x2="${width - padR}" y1="${ry}" y2="${ry}" stroke="${mutedColor}" stroke-width="1" stroke-dasharray="4,3" />`;
    if (refLine.label) {
      refLineSvg += `<text x="${width - padR}" y="${ry - 4}" text-anchor="end" font-size="10" fill="${mutedColor}">${refLine.label}</text>`;
    }
  }

  const xTicks = 5;
  let xAxisSvg = "";
  for (let i = 0; i <= xTicks; i++) {
    const t = tMin + ((tMax - tMin) * i) / xTicks;
    const xx = x(t);
    xAxisSvg += `<text x="${xx}" y="${height - 6}" text-anchor="middle" font-size="10" fill="${mutedColor}">${formatTick(new Date(t), hours)}</text>`;
  }

  function pathFor(points) {
    if (points.length === 0) return "";
    let d = "";
    points.forEach((p, i) => {
      const xx = x(p.t.getTime());
      const yy = y(p.v);
      if (i === 0) {
        d += `M ${xx} ${yy}`;
      } else if (stepped) {
        const prevY = y(points[i - 1].v);
        d += ` L ${xx} ${prevY} L ${xx} ${yy}`;
      } else {
        d += ` L ${xx} ${yy}`;
      }
    });
    return d;
  }

  let linesSvg = "";
  for (const s of series) {
    linesSvg += `<path d="${pathFor(s.points)}" fill="none" stroke="${cssVar(s.color)}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />`;
  }

  const svgId = "chart-" + Math.random().toString(36).slice(2, 9);
  container.innerHTML = `
    <svg class="chart" id="${svgId}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      ${gridSvg}
      ${refLineSvg}
      ${linesSvg}
      ${xAxisSvg}
      <g class="hover-layer" style="display:none">
        <line class="crosshair" y1="${padT}" y2="${padT + innerH}" stroke="${mutedColor}" stroke-width="1" stroke-dasharray="3,3" />
      </g>
      <rect x="${padL}" y="${padT}" width="${innerW}" height="${innerH}" fill="transparent" class="hover-target" />
    </svg>
    <div class="tooltip" style="position:relative;"></div>
  `;

  const svg = container.querySelector("svg");
  const hoverLayer = svg.querySelector(".hover-layer");
  const crosshair = svg.querySelector(".crosshair");
  const target = svg.querySelector(".hover-target");
  const tooltip = document.createElement("div");
  tooltip.style.cssText = `
    position: absolute; pointer-events: none; display: none;
    background: ${cssVar("--surface-1")}; border: 1px solid ${cssVar("--border")};
    border-radius: 8px; padding: 6px 10px; font-size: 12px; color: ${cssVar("--text-primary")};
    box-shadow: 0 2px 8px rgba(0,0,0,0.15); white-space: nowrap; z-index: 10;
  `;
  container.style.position = "relative";
  container.appendChild(tooltip);

  const sortedPoints = series[0].points;

  target.addEventListener("mousemove", (evt) => {
    const rect = svg.getBoundingClientRect();
    const scaleX = width / rect.width;
    const mouseX = (evt.clientX - rect.left) * scaleX;
    const t = tMin + ((mouseX - padL) / innerW) * (tMax - tMin);

    let closest = sortedPoints[0];
    let minDiff = Infinity;
    for (const p of sortedPoints) {
      const diff = Math.abs(p.t.getTime() - t);
      if (diff < minDiff) { minDiff = diff; closest = p; }
    }
    const idx = sortedPoints.indexOf(closest);
    const xx = x(closest.t.getTime());

    hoverLayer.style.display = "block";
    crosshair.setAttribute("x1", xx);
    crosshair.setAttribute("x2", xx);

    let rows = series
      .map((s) => {
        const p = s.points[idx];
        if (!p) return "";
        return `<div><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${cssVar(s.color)};margin-right:6px;"></span>${s.name}: <b>${p.v.toFixed(2)}${unit}</b></div>`;
      })
      .join("");

    tooltip.innerHTML = `<div style="color:${cssVar("--text-secondary")};margin-bottom:4px;">${closest.t.toLocaleString("en-GB")}</div>${rows}`;
    tooltip.style.display = "block";

    const evtRect = container.getBoundingClientRect();
    let left = ((xx / width) * evtRect.width) + 12;
    if (left + 160 > evtRect.width) left = ((xx / width) * evtRect.width) - 172;
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `12px`;
  });

  target.addEventListener("mouseleave", () => {
    hoverLayer.style.display = "none";
    tooltip.style.display = "none";
  });
}

function renderLegend(container, defs) {
  container.innerHTML = defs
    .map(
      (d) =>
        `<span><span class="swatch" style="background:${cssVar(d.color)}"></span>${d.name}</span>`
    )
    .join("");
}

async function fetchHistory(hours) {
  try {
    const res = await fetch(`/api/history?hours=${hours}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const points = data.points;

    const powerSeries = POWER_SERIES_DEFS.map((def) => ({
      name: def.name,
      color: def.color,
      points: points.map((p) => ({ t: new Date(p.timestamp), v: p[def.key] })),
    }));
    renderLegend(document.getElementById("powerLegend"), POWER_SERIES_DEFS);
    renderLineChart(document.getElementById("powerChartWrap"), powerSeries, { hours, unit: "kW", yMin: 0 });

    const stepSeries = [
      {
        name: "Step (Target)",
        color: "--series-step",
        points: points.map((p) => ({ t: new Date(p.timestamp), v: p.step })),
      },
    ];
    renderLineChart(document.getElementById("stepChartWrap"), stepSeries, {
      hours,
      stepped: true,
      height: 140,
      unit: "",
      yMin: 0,
      yMax: 7,
      yTickStep: 1,
      yDecimals: 0,
    });

    const loadSeries = [
      {
        name: "Load (Actual)",
        color: "--series-load",
        points: points.map((p) => ({ t: new Date(p.timestamp), v: p.heater_load_w })),
      },
    ];
    renderLineChart(document.getElementById("loadChartWrap"), loadSeries, {
      hours,
      height: 140,
      unit: "W",
      yMin: 0,
      yDecimals: 0,
    });

    const tempSeries = [
      {
        name: "Temperature",
        color: "--series-temp",
        points: points
          .filter((p) => p.askoheat_temp_c !== null && p.askoheat_temp_c !== undefined)
          .map((p) => ({ t: new Date(p.timestamp), v: p.askoheat_temp_c })),
      },
    ];
    renderLineChart(document.getElementById("tempChartWrap"), tempSeries, {
      hours,
      height: 140,
      unit: "°C",
      yMin: 0,
      yMax: 90,
      yDecimals: 1,
      refLine: { value: ASKOHEAT_MAX_TEMP_C, label: `Device limit ${ASKOHEAT_MAX_TEMP_C}°C` },
    });
  } catch (err) {
    document.getElementById("powerChartWrap").innerHTML = `<div class="empty-state">Failed to load history (${err.message})</div>`;
    document.getElementById("stepChartWrap").innerHTML = "";
    document.getElementById("loadChartWrap").innerHTML = "";
    document.getElementById("tempChartWrap").innerHTML = "";
  }
}

function setupRangeButtons() {
  const row = document.getElementById("rangeRow");
  row.addEventListener("click", (evt) => {
    const btn = evt.target.closest("button[data-hours]");
    if (!btn) return;
    row.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", "false"));
    btn.setAttribute("aria-pressed", "true");
    currentHours = parseInt(btn.dataset.hours, 10);
    fetchHistory(currentHours);
  });
}

function main() {
  setupRangeButtons();
  fetchLive();
  fetchHistory(currentHours);
  setInterval(fetchLive, 30000);
  setInterval(() => fetchHistory(currentHours), 60000);
}

main();

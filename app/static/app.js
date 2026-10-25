const TILE_DEFS = [
  { key: "pv_kw", label: "PV Production", unit: "kW", color: "--series-pv" },
  { key: "consumption_kw", label: "Consumption", unit: "kW", color: "--series-cons" },
  { key: "battery_charge_kw", label: "Battery (Charging)", unit: "kW", color: "--series-batt-charge" },
  { key: "battery_discharge_kw", label: "Battery (Discharging)", unit: "kW", color: "--series-batt-discharge" },
  { key: "battery_soc_pct", label: "Battery Charge Level", unit: "%", color: "--series-soc" },
  { key: "grid_import_kw", label: "Grid Import", unit: "kW", color: "--series-grid" },
  { key: "grid_export_kw", label: "Grid Export", unit: "kW", color: "--series-grid-export" },
];

const ASKO_TILE_DEFS = [
  { key: "step", label: "AskoHeat Step (Target)", unit: "/7", color: "--series-step" },
  { key: "heater_load_w", label: "AskoHeat Load (Actual)", unit: "W", color: "--series-load" },
  { key: "askoheat_temp_c", label: "AskoHeat Temperature", unit: "°C", color: "--series-temp" },
];

// SOC the battery has to reach before any surplus is used for heating -
// the battery is charged first (BATTERY_FULL_SOC_PCT in .env). Shown as a
// reference line on the charge level chart, which explains why the heater
// stays off on days when the battery never gets there.
const BATTERY_FULL_SOC_PCT = 95;

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
  // History rows keep the signed meter value, so the split happens here.
  // Drawing it signed under a y axis fixed at 0 is what used to push the
  // line out of the chart and across the card below it.
  { key: "grid_import_kw", name: "Grid Import", color: "--series-grid",
    value: (p) => Math.max(0, -p.grid_kw) },
  { key: "grid_export_kw", name: "Grid Export", color: "--series-grid-export",
    value: (p) => Math.max(0, p.grid_kw) },
  // Null wherever the charger has nothing to say: every row from before it
  // was wired up, and every poll it did not answer. Kept as null rather than
  // folded to 0 so the chart leaves a gap instead of claiming the car was
  // plugged in and drawing nothing.
  { key: "wallbox_power_kw", name: "Wallbox", color: "--series-wallbox",
    value: (p) => (p.wallbox_power_kw === null || p.wallbox_power_kw === undefined
      ? null
      : p.wallbox_power_kw) },
];

let currentHours = 24;

// The window the Power chart is showing, or null for the whole fetched
// range. Both live out here because the chart is rebuilt from scratch every
// 60 seconds and would otherwise snap back while being read.
let powerZoom = null;
let lastHistory = { points: [], hours: 24 };

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
    let subClass = "sub";
    // Keyed on the unit, not on a list of key names: the list silently
    // dropped every power tile added after it was written.
    if (def.unit === "kW") {
      valueText = `${fmt(latest[def.key])} ${def.unit}`;
    } else if (def.key === "battery_soc_pct") {
      valueText = latest.battery_soc_pct === null || latest.battery_soc_pct === undefined
        ? "-"
        : `${fmt(latest.battery_soc_pct, 0)} ${def.unit}`;
      // Don't claim a direction the measurement doesn't show: the
      // battery can hold priority while sitting idle.
      if (latest.battery_state === "charging") {
        sub = `Charging towards ${BATTERY_FULL_SOC_PCT}%`;
      } else if (latest.status === "battery") {
        sub = `Has priority up to ${BATTERY_FULL_SOC_PCT}%`;
      } else if (latest.battery_state === "discharging") {
        sub = "Discharging";
      }
    } else if (def.key === "step") {
      valueText = latest.step === null || latest.step === undefined
        ? "-"
        : `${latest.step}${def.unit}`;
      if (latest.status === "battery") {
        sub = "Battery has priority";
      } else if (latest.status === "capped") {
        sub = "Not heating (temperature limit)";
        subClass = "sub warning";
      } else if (latest.status === "warm") {
        sub = "Waiting to cool down";
        subClass = "sub warning";
      }
    } else if (def.key === "heater_load_w") {
      // Null whenever there is no reading at all - printing it raw put
      // "null W" on the dashboard.
      valueText = latest.heater_load_w === null || latest.heater_load_w === undefined
        ? "-"
        : `${latest.heater_load_w} ${def.unit}`;
    } else if (def.key === "askoheat_temp_c") {
      valueText = latest.askoheat_temp_c === null || latest.askoheat_temp_c === undefined
        ? "-"
        : `${fmt(latest.askoheat_temp_c, 1)} ${def.unit}`;
    }
    tile.innerHTML = `
      <div class="label"><span class="dot" style="background:${cssVar(def.color)}"></span>${def.label}</div>
      <div class="value">${valueText}</div>
      ${sub ? `<div class="${subClass}">${sub}</div>` : ""}
    `;
    wrap.appendChild(tile);
  }
}

// The wallbox panel is rendered by hand rather than through renderTiles:
// its values are a nested object with mixed shapes (triples of phase values,
// a raw-register map, an offline state) that the generic tile renderer has
// no reason to learn about.
function renderWallbox(wb, tariff) {
  const title = document.getElementById("wallboxTitle");
  const wrap = document.getElementById("tilesWallbox");
  const note = document.getElementById("wallboxNote");

  // No wallbox configured at all - hide the whole section rather than show
  // an empty one.
  if (!wb) {
    title.hidden = true;
    wrap.hidden = true;
    note.hidden = true;
    return;
  }
  title.hidden = false;
  wrap.hidden = false;
  note.hidden = false;
  wrap.innerHTML = "";

  // Restarted while a car was charging, so there is no snapshot to show and
  // reading one would interrupt the charge. An empty panel would read as a
  // fault, so name the reason.
  if (wb.awaiting_first_read) {
    const el = document.createElement("div");
    el.className = "tile";
    el.innerHTML = `
      <div class="label"><span class="dot" style="background:${cssVar("--series-wallbox")}"></span>Wallbox</div>
      <div class="value">Not read</div>
      <div class="sub">A car is charging and reading the charger would interrupt it</div>
    `;
    wrap.appendChild(el);
    note.textContent =
      "Every Modbus read drops this charger's current to 0 A for about two minutes, so " +
      "it is left alone while a car draws. The panel fills in once the charge finishes.";
    return;
  }

  const tile = (label, value, sub, subClass, color) => {
    const el = document.createElement("div");
    el.className = "tile";
    el.innerHTML = `
      <div class="label"><span class="dot" style="background:${cssVar(color || "--series-wallbox")}"></span>${label}</div>
      <div class="value">${value}</div>
      ${sub ? `<div class="${subClass || "sub"}">${sub}</div>` : ""}
    `;
    wrap.appendChild(el);
  };

  if (!wb.online) {
    tile("Wallbox", "Offline", wb.error || "No answer on Modbus TCP", "sub warning", "--status-error");
    note.textContent =
      "The wallbox allows only two Modbus sessions and one is normally taken by the SEMS cloud - " +
      "an offline reading here often just means both slots were busy.";
    return;
  }

  // Reading the charger cuts the charging current on this hardware, so the
  // poll is suspended while a car draws and these values stand still. Say so
  // rather than let the panel look stuck - see Controller._poll_wallbox.
  if (wb.poll_suspended) {
    note.textContent =
      "A car is charging, so the wallbox is not being read: every Modbus read drops " +
      "the charging current to 0 A for about two minutes on this hardware. The values " +
      "below are the last reading from before the charge started and will refresh once " +
      "it finishes.";
  } else {
    note.textContent =
      "The wallbox allows only two Modbus sessions and one is normally taken by the " +
      "SEMS cloud, so a reading here can fail when both slots are busy.";
  }

  // Charging is the one state worth colouring; everything else stays neutral
  // so a glance at the panel shows whether power is actually flowing.
  const statusColor = wb.charging ? "--success" : "--series-wallbox";
  const plugged = wb.vehicle_connected === null ? "" :
    wb.vehicle_connected ? "Vehicle connected" : "Nothing plugged in";
  tile("Status", wb.status_text || "-",
       wb.status_code === null ? plugged : `${plugged} · code ${wb.status_code}`,
       "sub", statusColor);

  const amps = wb.currents_a ? wb.currents_a.map((a) => `${fmt(a, 1)} A`).join(" · ") : "";
  tile("Charging Power", wb.power_kw === null ? "-" : `${fmt(wb.power_kw, 1)} kW`, amps);

  const volts = wb.voltages_v ? wb.voltages_v.map((v) => `${fmt(v, 1)} V`).join(" · ") : "-";
  tile("Grid Voltage", volts, "L1 · L2 · L3");

  const energyCost =
    tariff && wb.total_energy_kwh !== null
      ? `${(wb.total_energy_kwh * tariff.import_per_kwh).toFixed(2)} ${tariff.currency} at ${
          tariff.import_per_kwh
        }/kWh`
      : "";
  tile(
    "Total Energy",
    wb.total_energy_kwh === null ? "-" : `${fmt(wb.total_energy_kwh, 1)} kWh`,
    energyCost
  );

  tile("Charge Mode", wb.charge_mode || "-",
       wb.max_power_kw === null ? "" : `Limit ${fmt(wb.max_power_kw, 1)} kW`);

  tile("Hardware", [wb.power_spec, wb.phases].filter(Boolean).join(" · ") || "-",
       wb.phase_switching === null ? "" :
         wb.phase_switching ? "Phase switching enabled" : "Phase switching off");

  tile("RFID Card", wb.rfid_card || "None");

  // There is no documented alarm or error register in the community map, so
  // rather than invent one, the registers that answer but have no known
  // meaning are shown raw. Watching these change while a car charges is how
  // the alarm register would actually be found.
  const diag = wb.diagnostics || {};
  const keys = Object.keys(diag);
  if (keys.length) {
    tile("Undocumented Registers",
         `${keys.length} read`,
         keys.map((k) => `${k}=${diag[k]}`).join("  "),
         "sub mono");
  }

  note.textContent =
    "Read-only. Register map is community-derived (evcc) and not confirmed by GoodWe - " +
    "no alarm register is documented, so unknown registers are shown raw above.";
}

// Status codes that mean the surplus is actively being used (shown with the
// "good"/green accent).
const GOOD_STATUSES = new Set(["increasing", "max", "balanced"]);
// Status codes that need attention (shown with the "warning"/amber accent).
const WARNING_STATUSES = new Set(["capped", "warm"]);

function renderStatusBanner(latest) {
  const banner = document.getElementById("statusBanner");
  const text = document.getElementById("statusBannerText");
  banner.classList.remove("status-good", "status-warning", "status-error");
  if (!latest.status_text) {
    text.textContent = "No status available yet";
    return;
  }
  text.textContent = latest.status_text;
  if (latest.status === "error") {
    banner.classList.add("status-error");
  } else if (WARNING_STATUSES.has(latest.status)) {
    banner.classList.add("status-warning");
  } else if (GOOD_STATUSES.has(latest.status)) {
    banner.classList.add("status-good");
  }
}

// Every device here can be switched off, unplugged or simply not there
// yet, and the service keeps running without it - so the page has to say
// which ones are answering. The banner above carries one sentence and while
// the inverter is gone that sentence is about the inverter, which left a
// heater that was also off the network nowhere on the page.
const DEVICE_NAMES = {
  inverter: "Inverter",
  askoheat: "AskoHeat",
  wallbox: "Wallbox",
};

function deviceLine(latest) {
  if (!latest.devices) {
    return latest.battery_charging === undefined ? "" : "Live data";
  }
  const missing = Object.entries(latest.devices)
    .filter(([, d]) => d && !d.online)
    .map(([key]) => DEVICE_NAMES[key] || key);
  if (!missing.length) return "Live data - all devices answering";
  return `Live data - no answer from ${missing.join(", ")}`;
}

async function fetchLive() {
  try {
    const res = await fetch("/api/live");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const latest = await res.json();
    renderTiles("tiles", TILE_DEFS, latest);
    renderTiles("tilesAsko", ASKO_TILE_DEFS, latest);
    renderWallbox(latest.wallbox, latest.tariff);
    renderStatusBanner(latest);
    document.getElementById("statusLine").textContent = deviceLine(latest);
    document.getElementById("lastUpdate").textContent = new Date(latest.timestamp).toLocaleString("en-GB");
    if (latest.poll_interval_s !== undefined) {
      document.getElementById("updateInterval").textContent = latest.poll_interval_s;
    }
  } catch (err) {
    document.getElementById("statusLine").textContent = "No live data available (" + err.message + ")";
  }
}

// `hours` is the span actually on screen, not the span that was fetched -
// zoomed into two hours of a 30 day range, every tick would otherwise read
// the same date.
function formatTick(date, hours) {
  if (hours <= 0.5) {
    return date.toLocaleTimeString("en-GB", {
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  }
  if (hours <= 24) {
    return date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  }
  if (hours <= 24 * 4) {
    return date.toLocaleString("en-GB", {
      day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
    });
  }
  return date.toLocaleDateString("en-GB", { day: "2-digit", month: "2-digit" });
}

// Smallest window the chart will zoom to. At roughly one sample a minute,
// two minutes still has points in it; below that the user is just looking at
// the gaps between measurements.
const MIN_ZOOM_MS = 2 * 60 * 1000;

function clampWindow(from, to, lo, hi) {
  // A pointer position measured against a detached element comes back
  // non-finite; null means "no usable window", and every caller skips the
  // event rather than inventing one.
  if (!Number.isFinite(from) || !Number.isFinite(to)) return null;
  let span = Math.max(MIN_ZOOM_MS, to - from);
  span = Math.min(span, hi - lo);
  let a = Math.round(from + (to - from) / 2 - span / 2);
  a = Math.max(lo, Math.min(a, hi - span));
  return { from: a, to: a + span };
}

// Redraws the Power chart from the rows already in hand. Zooming never goes
// back to the server: /api/history hands over every recorded row for the
// range, so the detail is already here, and refetching 37k rows on each
// wheel tick would make the interaction unusable.
function renderPower() {
  const { points, hours } = lastHistory;
  const powerSeries = POWER_SERIES_DEFS.map((def) => ({
    name: def.name,
    color: def.color,
    points: points.map((p) => ({
      t: new Date(p.timestamp),
      v: def.value ? def.value(p) : p[def.key],
    })),
  }));
  renderLegend(document.getElementById("powerLegend"), POWER_SERIES_DEFS);
  renderLineChart(document.getElementById("powerChartWrap"), powerSeries, {
    hours,
    unit: "kW",
    yMin: 0,
    zoom: powerZoom,
    onZoom: (next) => {
      powerZoom = next;
      renderPower();
    },
  });
  renderPowerZoomBar();
}

function renderPowerZoomBar() {
  const bar = document.getElementById("powerZoomBar");
  const label = document.getElementById("powerZoomLabel");
  if (!bar || !label) return;
  if (!powerZoom) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  const from = new Date(powerZoom.from);
  const to = new Date(powerZoom.to);
  const minutes = Math.round((powerZoom.to - powerZoom.from) / 60000);
  const span = minutes < 90
    ? `${minutes} min`
    : `${(minutes / 60).toFixed(minutes < 60 * 24 ? 1 : 0)} h`;
  const sameDay = from.toDateString() === to.toDateString();
  const fmtT = (d) => d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  label.textContent = sameDay
    ? `${from.toLocaleDateString("en-GB")} ${fmtT(from)} - ${fmtT(to)} (${span})`
    : `${from.toLocaleString("en-GB")} - ${to.toLocaleString("en-GB")} (${span})`;
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
    // {from, to} in epoch ms, or null for the whole fetched range. Passing
    // onZoom is what turns the interaction on, so the other charts on this
    // page keep behaving exactly as before.
    zoom = null,
    onZoom = null,
  } = opts;
  const padL = 44, padR = 12, padT = 12, padB = 24;
  const innerW = width - padL - padR;
  const innerH = height - padT - padB;

  const allPoints = series.flatMap((s) => s.points);
  if (allPoints.length === 0) {
    container.innerHTML = '<div class="empty-state">No history data for this time range yet.</div>';
    return;
  }

  // Looped rather than Math.min(...array): each argument of a spread goes
  // on the stack, and 30 days across seven series is a few hundred thousand
  // of them - "Maximum call stack size exceeded", which the page reported as
  // "Failed to load history" and an empty card.
  const extent = (values) => {
    let lo = Infinity;
    let hi = -Infinity;
    for (const v of values) {
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    return [lo, hi];
  };

  const [dataT0, dataT1] = extent(allPoints.map((p) => p.t.getTime()));
  // The zoom window is clamped to the data rather than trusted: a stale
  // window from a wider range would otherwise scroll the chart off its own
  // axis after the range buttons narrowed the fetch.
  const win = zoom ? clampWindow(zoom.from, zoom.to, dataT0, dataT1) : null;
  const tMin = win ? win.from : dataT0;
  const tMax = win ? win.to : dataT1;
  const visibleHours = (tMax - tMin) / 3600000;

  // Scaled to what is on screen, not to the whole fetch. Without this,
  // zooming into a quiet night still measures the axis against a 28kW import
  // spike from the previous afternoon and every line stays flat on the
  // baseline - which is exactly the detail the zoom was supposed to reveal.
  const inWindow = (p) => {
    const t = p.t.getTime();
    return t >= tMin && t <= tMax;
  };

  // A series may be missing values in the middle without being shorter than
  // the others: the tooltip reads every series at the index it found in the
  // first one, so dropping the rows instead would line each series up
  // against the wrong timestamp.
  const known = allPoints.filter((p) => p.v !== null && p.v !== undefined);

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
    // Both come back as +/-Infinity when nothing is known, which the
    // vMin === vMax branch below turns into a plain 0..1 axis.
    const [dataMin, dataMax] = extent(known.filter(inWindow).map((p) => p.v));
    vMin = yMinOpt !== undefined ? yMinOpt : Math.min(0, dataMin);
    vMax = yMaxOpt !== undefined ? yMaxOpt : Math.max(0, dataMax);
    if (!Number.isFinite(vMin)) vMin = 0;
    if (!Number.isFinite(vMax)) vMax = 0;
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
    xAxisSvg += `<text x="${xx}" y="${height - 6}" text-anchor="middle" font-size="10" fill="${mutedColor}">${formatTick(new Date(t), visibleHours)}</text>`;
  }

  function pathFor(points) {
    if (points.length === 0) return "";
    let d = "";
    // `pen` is false whenever the previous point was missing, so the line
    // restarts with a move instead of jumping across the gap. Without it one
    // null produced `L x NaN` and the browser dropped the rest of the path.
    let pen = false;
    points.forEach((p, i) => {
      if (p.v === null || p.v === undefined) {
        pen = false;
        return;
      }
      const xx = x(p.t.getTime());
      const yy = y(p.v);
      if (!pen) {
        d += ` M ${xx} ${yy}`;
      } else if (stepped) {
        const prevY = y(points[i - 1].v);
        d += ` L ${xx} ${prevY} L ${xx} ${yy}`;
      } else {
        d += ` L ${xx} ${yy}`;
      }
      pen = true;
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
      <defs><clipPath id="clip-${svgId}">
        <rect x="${padL}" y="${padT - 2}" width="${innerW}" height="${innerH + 4}" />
      </clipPath></defs>
      ${gridSvg}
      ${refLineSvg}
      <g clip-path="url(#clip-${svgId})">${linesSvg}</g>
      ${xAxisSvg}
      <g class="hover-layer" style="display:none">
        <line class="crosshair" y1="${padT}" y2="${padT + innerH}" stroke="${mutedColor}" stroke-width="1" stroke-dasharray="3,3" />
      </g>
      <rect class="zoom-band" y="${padT}" height="${innerH}" width="0" x="${padL}"
            fill="${cssVar("--text-primary")}" opacity="0.12" style="display:none" />
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

    // Only points inside the window: zoomed in, the nearest sample overall
    // can be one that is scrolled off screen, and the crosshair would be
    // drawn outside the plot for a reading the user cannot see.
    let closest = null;
    let minDiff = Infinity;
    for (const p of sortedPoints) {
      const pt = p.t.getTime();
      if (pt < tMin || pt > tMax) continue;
      const diff = Math.abs(pt - t);
      if (diff < minDiff) { minDiff = diff; closest = p; }
    }
    if (!closest) return;
    const idx = sortedPoints.indexOf(closest);
    const xx = x(closest.t.getTime());

    hoverLayer.style.display = "block";
    crosshair.setAttribute("x1", xx);
    crosshair.setAttribute("x2", xx);

    let rows = series
      .map((s) => {
        const p = s.points[idx];
        // No row, or a row the device had nothing to say about - leave the
        // series out of the tooltip rather than print a nonexistent 0.
        if (!p || p.v === null || p.v === undefined) return "";
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

  if (!onZoom) return;

  // --- zoom -----------------------------------------------------------
  // Three ways in, because none of them is discoverable on its own: drag a
  // span, wheel towards a point, double-click to come back out.
  const band = svg.querySelector(".zoom-band");
  const toTime = (clientX) => {
    const rect = svg.getBoundingClientRect();
    const px = (clientX - rect.left) * (width / rect.width);
    return tMin + ((px - padL) / innerW) * (tMax - tMin);
  };
  let dragFrom = null;

  target.style.cursor = "ew-resize";

  target.addEventListener("mousedown", (evt) => {
    if (evt.button !== 0) return;
    evt.preventDefault();
    dragFrom = toTime(evt.clientX);
    band.style.display = "block";
    tooltip.style.display = "none";
  });

  target.addEventListener("mousemove", (evt) => {
    if (dragFrom === null) return;
    const a = x(Math.min(dragFrom, toTime(evt.clientX)));
    const b = x(Math.max(dragFrom, toTime(evt.clientX)));
    band.setAttribute("x", Math.max(padL, a));
    band.setAttribute("width", Math.max(0, Math.min(b, padL + innerW) - Math.max(padL, a)));
    // The crosshair and tooltip would fight the selection for attention.
    hoverLayer.style.display = "none";
    tooltip.style.display = "none";
  });

  const endDrag = (evt) => {
    if (dragFrom === null) return;
    const to = toTime(evt.clientX);
    const from = dragFrom;
    dragFrom = null;
    band.style.display = "none";
    band.setAttribute("width", 0);
    // A plain click is not a selection - without this every click on the
    // chart would zoom to a zero-width window.
    if (!Number.isFinite(from) || !Number.isFinite(to)) return;
    if (Math.abs(to - from) < MIN_ZOOM_MS / 4) return;
    const next = clampWindow(Math.min(from, to), Math.max(from, to), dataT0, dataT1);
    if (next) onZoom(next);
  };
  target.addEventListener("mouseup", endDrag);
  // Released outside the plot area: still a finished drag, not a stuck one.
  window.addEventListener("mouseup", endDrag);

  target.addEventListener("wheel", (evt) => {
    evt.preventDefault();
    const focus = toTime(evt.clientX);
    if (!Number.isFinite(focus)) return;
    const factor = evt.deltaY < 0 ? 0.7 : 1 / 0.7;
    const span = (tMax - tMin) * factor;
    if (span >= dataT1 - dataT0) {
      onZoom(null);
      return;
    }
    // Keep whatever is under the pointer under the pointer.
    const share = (focus - tMin) / (tMax - tMin);
    const next = clampWindow(focus - span * share, focus + span * (1 - share), dataT0, dataT1);
    if (next) onZoom(next);
  }, { passive: false });

  target.addEventListener("dblclick", () => onZoom(null));
}

// Maps every controller status code to a display category so the timeline
// and legend stay in sync. "good" = surplus actively used, "battery" = the
// battery is being charged first and heating deliberately waits, "warning" =
// commanded but blocked/not heating, "error" = control failure, "neutral" =
// everything else (off/decreasing/holding, and missing status on old rows
// from before status tracking was added).
const STATUS_CATEGORY = {
  increasing: "good",
  max: "good",
  balanced: "good",
  battery: "battery",
  capped: "warning",
  warm: "warning",
  error: "error",
};
const STATUS_CATEGORY_COLOR = {
  good: "--success",
  battery: "--series-soc",
  warning: "--status-warning",
  error: "--status-error",
  neutral: "--baseline",
};
const STATUS_LEGEND_DEFS = [
  { name: "Heating from surplus", color: STATUS_CATEGORY_COLOR.good },
  { name: "Charging battery (priority)", color: STATUS_CATEGORY_COLOR.battery },
  { name: "Blocked / not heating", color: STATUS_CATEGORY_COLOR.warning },
  { name: "Control error", color: STATUS_CATEGORY_COLOR.error },
  { name: "Idle / no surplus", color: STATUS_CATEGORY_COLOR.neutral },
];

function categoryFor(status) {
  return STATUS_CATEGORY[status] || "neutral";
}

// Renders a horizontal colored-segment timeline for the controller's status
// over time, sharing the same time axis convention as renderLineChart so it
// lines up visually with the charts above/below it.
function renderStatusTimeline(container, points, opts) {
  const { width = 960, height = 60, hours } = opts;
  const padL = 44, padR = 12, padT = 6, padB = 22;
  const innerW = width - padL - padR;
  const barY = padT;
  const barH = height - padT - padB;

  if (points.length === 0) {
    container.innerHTML = '<div class="empty-state">No history data for this time range yet.</div>';
    return;
  }

  const tMin = points[0].t.getTime();
  const tMax = Math.max(points[points.length - 1].t.getTime(), tMin + 1);
  const x = (t) => padL + ((t - tMin) / (tMax - tMin)) * innerW;

  let segmentsSvg = "";
  points.forEach((p, i) => {
    const startX = x(p.t.getTime());
    const endT = i + 1 < points.length ? points[i + 1].t.getTime() : tMax;
    const endX = Math.max(x(endT), startX + 1);
    const color = cssVar(STATUS_CATEGORY_COLOR[categoryFor(p.status)]);
    segmentsSvg += `<rect x="${startX}" y="${barY}" width="${endX - startX}" height="${barH}" fill="${color}" />`;
  });

  const mutedColor = cssVar("--text-muted");
  const xTicks = 5;
  let xAxisSvg = "";
  for (let i = 0; i <= xTicks; i++) {
    const t = tMin + ((tMax - tMin) * i) / xTicks;
    xAxisSvg += `<text x="${x(t)}" y="${height - 6}" text-anchor="middle" font-size="10" fill="${mutedColor}">${formatTick(new Date(t), hours)}</text>`;
  }

  container.innerHTML = `
    <svg class="chart" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      <rect x="${padL}" y="${barY}" width="${innerW}" height="${barH}" rx="3" fill="${cssVar("--gridline")}" />
      ${segmentsSvg}
      ${xAxisSvg}
      <g class="hover-layer" style="display:none">
        <line class="crosshair" y1="${barY}" y2="${barY + barH}" stroke="${mutedColor}" stroke-width="1" stroke-dasharray="3,3" />
      </g>
      <rect x="${padL}" y="${barY}" width="${innerW}" height="${barH}" fill="transparent" class="hover-target" />
    </svg>
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
    box-shadow: 0 2px 8px rgba(0,0,0,0.15); white-space: normal; max-width: 260px; z-index: 10;
  `;
  container.style.position = "relative";
  container.appendChild(tooltip);

  target.addEventListener("mousemove", (evt) => {
    const rect = svg.getBoundingClientRect();
    const scaleX = width / rect.width;
    const mouseX = (evt.clientX - rect.left) * scaleX;
    const t = tMin + ((mouseX - padL) / innerW) * (tMax - tMin);

    let closest = points[0];
    let minDiff = Infinity;
    for (const p of points) {
      const diff = Math.abs(p.t.getTime() - t);
      if (diff < minDiff) { minDiff = diff; closest = p; }
    }
    const xx = x(closest.t.getTime());

    hoverLayer.style.display = "block";
    crosshair.setAttribute("x1", xx);
    crosshair.setAttribute("x2", xx);

    const color = cssVar(STATUS_CATEGORY_COLOR[categoryFor(closest.status)]);
    tooltip.innerHTML = `
      <div style="color:${cssVar("--text-secondary")};margin-bottom:4px;">${closest.t.toLocaleString("en-GB")}</div>
      <div><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${color};margin-right:6px;"></span>${closest.status_text || closest.status || "No status recorded (before this feature)"}</div>
    `;
    tooltip.style.display = "block";

    const evtRect = container.getBoundingClientRect();
    let left = ((xx / width) * evtRect.width) + 12;
    if (left + 260 > evtRect.width) left = ((xx / width) * evtRect.width) - 272;
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `4px`;
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

    lastHistory = { points, hours };
    renderPower();

    // Rows from before the battery-priority logic have no SOC - filtered
    // out rather than drawn as 0, which would look like an empty battery.
    const socSeries = [
      {
        name: "Charge Level",
        color: "--series-soc",
        points: points
          .filter((p) => p.battery_soc_pct !== null && p.battery_soc_pct !== undefined)
          .map((p) => ({ t: new Date(p.timestamp), v: p.battery_soc_pct })),
      },
    ];
    renderLineChart(document.getElementById("socChartWrap"), socSeries, {
      hours,
      height: 140,
      unit: "%",
      yMin: 0,
      yMax: 100,
      yDecimals: 0,
      refLine: { value: BATTERY_FULL_SOC_PCT, label: `Heating from ${BATTERY_FULL_SOC_PCT}%` },
    });

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

    // Keep every point (not just ones with a status) so the time axis lines
    // up with the other charts above/below it - rows from before status
    // tracking existed just render as the neutral/gray category.
    const statusPoints = points.map((p) => ({
      t: new Date(p.timestamp),
      status: p.status,
      status_text: p.status_text,
    }));
    renderLegend(document.getElementById("statusLegend"), STATUS_LEGEND_DEFS);
    renderStatusTimeline(document.getElementById("statusTimelineWrap"), statusPoints, { hours });

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
    document.getElementById("socChartWrap").innerHTML = "";
    document.getElementById("stepChartWrap").innerHTML = "";
    document.getElementById("loadChartWrap").innerHTML = "";
    document.getElementById("tempChartWrap").innerHTML = "";
  }
}

function setupRangeButtons() {
  const zoomReset = document.getElementById("powerZoomReset");
  if (zoomReset) {
    zoomReset.addEventListener("click", () => {
      powerZoom = null;
      renderPower();
    });
  }

  const row = document.getElementById("rangeRow");
  row.addEventListener("click", (evt) => {
    const btn = evt.target.closest("button[data-hours]");
    if (!btn) return;
    row.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", "false"));
    btn.setAttribute("aria-pressed", "true");
    currentHours = parseInt(btn.dataset.hours, 10);
    // Picking a range is asking for that range, not for the old window
    // inside it.
    powerZoom = null;
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

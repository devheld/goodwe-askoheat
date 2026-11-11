// Energie Grafik - the Home Energy Manager as a chart widget (Scriptable app).
//
// The companion to "Energie": the same readings, drawn instead of listed.
// Built for the large size (4x4), the largest widget an iPhone has.
//
//   Jetzt     two stacked bars on one scale: where the power comes from
//             (PV, battery, grid) and where it goes (house, wallbox, heat
//             pump, battery, export), with the values under each bar.
//   Heute     where today's consumption came from (PV, battery, grid) as a
//             donut, autarky in the middle.
//   Akku      the charge level as a ring.
//   Stunden   today hour by hour: generation above the line, consumption
//             below it, split by where it came from, on one scale.
//
// Charts are pictures and do not switch with the phone's light and dark
// appearance, so only the marks are drawn, in colours checked against both
// backgrounds; every text is a widget text and does switch.
//
// Served by the Pi at /widget/grafik.js with HOSTS filled in, and updates
// itself from there like "Energie" does.

const HOSTS = /*__HOSTS__*/[];
const SCRIPT_PATH = "/widget/grafik.js";
const DATA_PATH = "/api/widget/graph";
const TIMEOUT_S = 8;
const STALE_MIN = 10;
const REFRESH_MIN = 5;
const CACHE_FILE = "energie-grafik-cache.json";

// ------------------------------------------------------------ colours
const dyn = (light, dark) => Color.dynamic(new Color(light), new Color(dark));
const C = {
  bg: dyn("#ffffff", "#1c1c1e"),
  tile: dyn("#f2f2f7", "#2c2c2e"),
  ink: dyn("#1c1c1e", "#f2f2f7"),
  ink2: dyn("#6e6e73", "#98989f"),
  warn: dyn("#b35c00", "#ff9f0a"),
};

// The marks: one set for both appearances, each validated on the light and
// the dark tile. Sources (PV, battery, grid) and sinks (house, wallbox,
// heat pump, battery, export) are checked as the groups they appear in; the
// battery keeps its blue in both, because it is the same thing.
const S = {
  pv: new Color("#c86e05"),
  batt: new Color("#3f8ee8"),
  grid: new Color("#cf4f8c"),
  house: new Color("#8e8e93"),
  wb: new Color("#8d5bd8"),
  hp: new Color("#c2416f"),
  exp: new Color("#2fa84f"),
  track: new Color("#8e8e93", 0.25),
  axis: new Color("#8e8e93", 0.7),
};

// One size per kind of text, as in "Energie".
const F = {
  head: Font.semiboldSystemFont(13),
  title: Font.semiboldSystemFont(12),
  label: Font.mediumSystemFont(11),
  value: Font.semiboldRoundedSystemFont(11),
  big: Font.boldRoundedSystemFont(15),
};

// ------------------------------------------------------------ formatting
function power(kw) {
  if (kw === null || kw === undefined || isNaN(kw)) return "–";
  const w = Math.abs(kw) * 1000;
  if (w < 10) return "0 W";
  if (w < 1000) return `${Math.round(w)} W`;
  return `${(w / 1000).toFixed(1)} kW`;
}

// Chip values: the card title states the unit, so the number stands alone.
function kw(v) {
  const a = Math.abs(v || 0);
  if (a < 0.01) return "0";
  return a < 1 ? a.toFixed(2) : a.toFixed(1);
}

const kwh = (v) => (v === null || v === undefined ? "–" : v >= 100 ? `${Math.round(v)}` : v.toFixed(1));
const pct = (v) => (v === null || v === undefined ? "–" : `${Math.round(v)} %`);
const sum = (parts) => parts.reduce((a, p) => a + Math.max(0, p.value || 0), 0);

function parseTs(ts) {
  return new Date(String(ts).replace(/(\.\d{3})\d+/, "$1"));
}

function clock(date) {
  const df = new DateFormatter();
  df.dateFormat = "HH:mm";
  return df.string(date);
}

function freshness(state) {
  const ts = parseTs(state.data.timestamp);
  const ageMin = (Date.now() - ts.getTime()) / 60000;
  if (state.offline) return { text: `offline · ${clock(ts)}`, warn: true };
  if (ageMin > STALE_MIN) return { text: `alt · ${clock(ts)}`, warn: true };
  return { text: clock(ts), warn: false };
}

// ------------------------------------------------------------ data
function firstSuccess(promises) {
  return new Promise((resolve, reject) => {
    if (!promises.length) return reject(new Error("no address"));
    let failed = 0;
    for (const p of promises) {
      p.then(resolve, () => {
        failed += 1;
        if (failed === promises.length) reject(new Error("offline"));
      });
    }
  });
}

async function fetchFrom(host) {
  const req = new Request(`${host}${DATA_PATH}`);
  req.timeoutInterval = TIMEOUT_S;
  const data = await req.loadJSON();
  if (!req.response || req.response.statusCode !== 200 || !data || !data.timestamp) {
    throw new Error("bad answer");
  }
  return { host, data };
}

const fm = FileManager.local();
const cachePath = fm.joinPath(fm.documentsDirectory(), CACHE_FILE);

async function load() {
  try {
    const got = await firstSuccess(HOSTS.map(fetchFrom));
    try {
      fm.writeString(cachePath, JSON.stringify(got));
    } catch (e) {
      // Without a cache the live reading still shows.
    }
    return { ...got, offline: false };
  } catch (e) {
    try {
      const cached = fm.fileExists(cachePath) ? JSON.parse(fm.readString(cachePath)) : null;
      return cached ? { ...cached, offline: true } : null;
    } catch (e2) {
      return null;
    }
  }
}

// ------------------------------------------------------------ drawing
function canvas(w, h) {
  const ctx = new DrawContext();
  ctx.size = new Size(w, h);
  ctx.opaque = false;
  ctx.respectScreenScale = true;
  return ctx;
}

function fillPolygon(ctx, points, color) {
  const path = new Path();
  path.move(points[0]);
  for (let i = 1; i < points.length; i++) path.addLine(points[i]);
  path.closeSubpath();
  ctx.addPath(path);
  ctx.setFillColor(color);
  ctx.fillPath();
}

// An annular sector, angles from 12 o'clock clockwise. Path has no arcs,
// so the edges are polygons at two degrees a step - smooth at this size.
function ringSegment(ctx, cx, cy, rOut, rIn, a0, a1, color) {
  if (a1 - a0 < 0.002) return;
  const n = Math.max(3, Math.ceil((a1 - a0) / (Math.PI / 90)));
  const pts = [];
  for (let i = 0; i <= n; i++) {
    const a = a0 + ((a1 - a0) * i) / n;
    pts.push(new Point(cx + rOut * Math.sin(a), cy - rOut * Math.cos(a)));
  }
  for (let i = n; i >= 0; i--) {
    const a = a0 + ((a1 - a0) * i) / n;
    pts.push(new Point(cx + rIn * Math.sin(a), cy - rIn * Math.cos(a)));
  }
  fillPolygon(ctx, pts, color);
}

// A bar's outer end rounded, its base square: the base sits on the axis.
function bar(ctx, x, y, w, h, color, end) {
  ctx.setFillColor(color);
  const r = Math.min(3, w / 2, h / 2);
  if (r < 1) {
    ctx.fillRect(new Rect(x, y, w, h));
    return;
  }
  const path = new Path();
  path.addRoundedRect(new Rect(x, y, w, h), r, r);
  ctx.addPath(path);
  ctx.fillPath();
  if (end === "top") ctx.fillRect(new Rect(x, y + h / 2, w, h / 2));
  else if (end === "bottom") ctx.fillRect(new Rect(x, y, w, h / 2));
  else if (end === "right") ctx.fillRect(new Rect(x, y, w / 2, h));
}

const GAP_PT = 2; // the surface gap between touching fills

function stackedBarImage(w, h, parts, scale) {
  const ctx = canvas(w, h);
  const track = new Path();
  track.addRoundedRect(new Rect(0, 0, w, h), 3, 3);
  ctx.addPath(track);
  ctx.setFillColor(S.track);
  ctx.fillPath();
  // The same threshold as the values under the bar: no segment without
  // its number.
  const shown = parts.filter((p) => (p.value || 0) >= 0.01 && (p.value / scale) * w >= 1);
  let x = 0;
  shown.forEach((p, i) => {
    const span = (p.value / scale) * w;
    const last = i === shown.length - 1;
    const segW = Math.max(1, span - (last ? 0 : GAP_PT));
    if (last) bar(ctx, x, 0, segW, h, p.color, "right");
    else {
      ctx.setFillColor(p.color);
      ctx.fillRect(new Rect(x, 0, segW, h));
    }
    x += span;
  });
  return ctx.getImage();
}

function donutImage(d, parts, thickness) {
  const ctx = canvas(d, d);
  const c = d / 2;
  const rOut = d / 2;
  const rIn = rOut - thickness;
  const total = sum(parts);
  if (total <= 0) {
    ringSegment(ctx, c, c, rOut, rIn, 0, 2 * Math.PI, S.track);
    return ctx.getImage();
  }
  const shown = parts.filter((p) => (p.value || 0) / total > 0.004);
  const gap = shown.length > 1 ? GAP_PT / ((rOut + rIn) / 2) : 0;
  let a = 0;
  for (const p of shown) {
    const span = (p.value / total) * 2 * Math.PI;
    ringSegment(ctx, c, c, rOut, rIn, a + gap / 2, a + span - gap / 2, p.color);
    a += span;
  }
  return ctx.getImage();
}

function gaugeImage(d, fraction, color, thickness) {
  const ctx = canvas(d, d);
  const c = d / 2;
  const f = Math.max(0, Math.min(1, fraction || 0));
  ringSegment(ctx, c, c, d / 2, d / 2 - thickness, 0, 2 * Math.PI, S.track);
  ringSegment(ctx, c, c, d / 2, d / 2 - thickness, 0, f * 2 * Math.PI, color);
  return ctx.getImage();
}

// Where an hour's (or the day's) consumption came from. The grid first,
// then the battery, and PV is what is left: the inverter's PV figure also
// covers what went into the battery or out to the grid, so it cannot be
// read off directly. Import beyond consumption went into the battery.
function origin(consumption, imported, discharged) {
  const cons = Math.max(0, consumption || 0);
  const grid = Math.min(Math.max(0, imported || 0), cons);
  const batt = Math.min(Math.max(0, discharged || 0), cons - grid);
  return { pv: cons - grid - batt, batt, grid };
}

function hourlyImage(w, h, hours) {
  const ctx = canvas(w, h);
  const slot = w / 24;
  const bw = Math.max(2, slot - 3);
  // gen is what the roof made; pv (from origin) is the part of the
  // consumption it covered. Different numbers, so different names.
  const rows = (hours || []).map((x) => ({
    h: x.h,
    gen: Math.max(0, x.pv || 0),
    ...origin(x.consumption, x.import, x.discharge),
  }));
  const maxUp = Math.max(0, ...rows.map((r) => r.gen));
  const maxDown = Math.max(0, ...rows.map((r) => r.pv + r.batt + r.grid));
  if (maxUp + maxDown <= 0) {
    ctx.setFillColor(S.axis);
    ctx.fillRect(new Rect(0, h / 2, w, 1));
    return ctx.getImage();
  }
  // One scale above and below the line: a kWh is the same height on
  // either side, so the line sits where the two maxima put it.
  const scale = (h - 1) / (maxUp + maxDown);
  const base = Math.round(maxUp * scale);
  for (const r of rows) {
    const x = r.h * slot + (slot - bw) / 2;
    if (r.gen * scale >= 0.5) {
      const hp = r.gen * scale;
      bar(ctx, x, base - hp, bw, hp, S.pv, "top");
    }
    const segs = [
      [r.pv, S.pv],
      [r.batt, S.batt],
      [r.grid, S.grid],
    ].filter(([v]) => v * scale >= 0.5);
    let y = base + 1;
    segs.forEach(([v, color], i) => {
      const span = v * scale;
      const last = i === segs.length - 1;
      const segH = last || span < 3 ? span : span - GAP_PT / 2;
      if (last) bar(ctx, x, y, bw, segH, color, "bottom");
      else {
        ctx.setFillColor(color);
        ctx.fillRect(new Rect(x, y, bw, segH));
      }
      y += span;
    });
  }
  ctx.setFillColor(S.axis);
  ctx.fillRect(new Rect(0, base, w, 1));
  return ctx.getImage();
}

// ------------------------------------------------------------ widget pieces
function addText(stack, str, font, color, scale = 0.85) {
  const t = stack.addText(str);
  t.font = font;
  t.textColor = color;
  t.lineLimit = 1;
  t.minimumScaleFactor = scale;
  return t;
}

function dot(stack, color) {
  const ctx = canvas(8, 8);
  ctx.setFillColor(color);
  ctx.fillEllipse(new Rect(0, 0, 8, 8));
  const img = stack.addImage(ctx.getImage());
  img.imageSize = new Size(8, 8);
}

function tile(parent, width, height) {
  const t = parent.addStack();
  t.layoutVertically();
  t.size = new Size(width, height || 0);
  t.backgroundColor = C.tile;
  t.cornerRadius = 12;
  t.setPadding(8, 9, 8, 9);
  return t;
}

const CHAR_W = 6.4; // estimated width of an 11 pt character
const ROW_H = 14; // one line of 11 pt text

function chipWidth(c) {
  return 8 + 4 + (c.label.length + 1 + c.value.length) * CHAR_W + 10;
}

function chipRows(chips, width) {
  const rows = [[]];
  let used = 0;
  for (const c of chips) {
    const w = chipWidth(c);
    if (used + w > width && rows[rows.length - 1].length) {
      rows.push([]);
      used = 0;
    }
    rows[rows.length - 1].push(c);
    used += w;
  }
  return rows;
}

function addChips(parent, rows) {
  for (const row of rows) {
    const r = parent.addStack();
    r.centerAlignContent();
    r.spacing = 10;
    for (const c of row) {
      const chip = r.addStack();
      chip.centerAlignContent();
      dot(chip, c.color);
      chip.addSpacer(4);
      addText(chip, c.label, F.label, C.ink2);
      chip.addSpacer(3);
      addText(chip, c.value, F.value, C.ink);
    }
  }
}

function widgetSize() {
  const SIZES = [
    [428, 364, 382],
    [414, 360, 379],
    [402, 348, 362],
    [390, 338, 354],
    [375, 329, 345],
    [0, 292, 311],
  ];
  const sw = Device.screenSize().width;
  const [, w, h] = SIZES.find(([min]) => sw >= min);
  return new Size(w, h);
}

// ------------------------------------------------------------ the cards
const LABEL_W = 46;
const BAR_H = 10;

function flowParts(d) {
  const hp = Math.max(0, d.heatpump_kw || 0);
  const wb = Math.max(0, d.wallbox_kw || 0);
  const battery = d.battery_kw || 0;
  return {
    from: [
      { label: "PV", value: Math.max(0, d.pv_kw || 0), color: S.pv },
      { label: "Akku", value: Math.max(0, -battery), color: S.batt },
      { label: "Netz", value: Math.max(0, d.grid_import_kw || 0), color: S.grid },
    ],
    // The house figure is the rest once the metered loads are off it, so
    // the AskoHeat heater is part of it.
    to: [
      { label: "Haus", value: Math.max(0, (d.consumption_kw || 0) - hp - wb), color: S.house },
      { label: "Wallbox", value: wb, color: S.wb },
      { label: "WP", value: hp, color: S.hp },
      { label: "Akku", value: Math.max(0, battery), color: S.batt },
      { label: "Einsp.", value: Math.max(0, d.grid_export_kw || 0), color: S.exp },
    ],
  };
}

function chipsOf(parts) {
  return parts
    .filter((p) => (p.value || 0) >= 0.01)
    .map((p) => ({ label: p.label, value: kw(p.value), color: p.color }));
}

// Laid out before it is drawn, so its height is known and the hourly
// chart can have what is left.
function planFlow(d, width) {
  const inner = width - 18;
  const parts = flowParts(d);
  const fromRows = chipRows(chipsOf(parts.from), inner - LABEL_W);
  const toRows = chipRows(chipsOf(parts.to), inner - LABEL_W);
  const group = (rows) => BAR_H + 4 + Math.max(1, rows.length) * ROW_H;
  const height = 16 + 15 + 6 + group(fromRows) + 7 + group(toRows);
  return { parts, fromRows, toRows, inner, height };
}

function flowCard(parent, plan, width) {
  const card = tile(parent, width, plan.height);
  const head = card.addStack();
  head.centerAlignContent();
  addText(head, "Jetzt", F.title, C.ink);
  head.addSpacer();
  addText(head, "kW", F.label, C.ink2);
  card.addSpacer(6);

  const scale = Math.max(sum(plan.parts.from), sum(plan.parts.to), 0.05);
  const group = (name, parts, rows) => {
    const row = card.addStack();
    row.centerAlignContent();
    const label = row.addStack();
    label.size = new Size(LABEL_W, 0);
    addText(label, name, F.label, C.ink2);
    const w = plan.inner - LABEL_W;
    const img = row.addImage(stackedBarImage(w, BAR_H, parts, scale));
    img.imageSize = new Size(w, BAR_H);
    card.addSpacer(4);
    const chips = card.addStack();
    chips.layoutVertically();
    chips.setPadding(0, LABEL_W, 0, 0);
    if (rows.length && rows[0].length) addChips(chips, rows);
    else addText(chips, "nichts", F.label, C.ink2);
  };
  group("Woher", plan.parts.from, plan.fromRows);
  card.addSpacer(7);
  group("Wohin", plan.parts.to, plan.toRows);
}

function donutCard(parent, d, width, height) {
  const card = tile(parent, width, height);
  const t = d.today || {};
  const o = origin(t.consumption_kwh, t.import_kwh, t.battery_discharge_kwh);
  const parts = [
    { label: "PV", value: o.pv, color: S.pv },
    { label: "Akku", value: o.batt, color: S.batt },
    { label: "Netz", value: o.grid, color: S.grid },
  ];

  const head = card.addStack();
  head.centerAlignContent();
  addText(head, "Heute", F.title, C.ink);
  head.addSpacer();
  addText(head, `${pct(t.autarky_pct)} autark`, F.label, C.ink2);
  card.addSpacer();

  // The donut's middle holds what its parts add up to: the day's
  // consumption. Its unit goes under it where the ring is large enough.
  const body = card.addStack();
  body.centerAlignContent();
  const dia = Math.min(height - 16 - 15 - 4, 70);
  const ring = body.addStack();
  ring.size = new Size(dia, dia);
  ring.backgroundImage = donutImage(dia, parts, 9);
  ring.layoutVertically();
  ring.addSpacer();
  const l1 = ring.addStack();
  l1.addSpacer();
  addText(l1, kwh(t.consumption_kwh), F.big, C.ink);
  l1.addSpacer();
  if (dia >= 58) {
    const l2 = ring.addStack();
    l2.addSpacer();
    addText(l2, "kWh", F.label, C.ink2);
    l2.addSpacer();
  }
  ring.addSpacer();

  body.addSpacer(10);
  const legend = body.addStack();
  legend.layoutVertically();
  legend.spacing = 4;
  // A fixed width, so the spacer in each row has room to push the number
  // to the right edge.
  legend.size = new Size(width - 18 - dia - 10, 0);
  for (const p of parts) {
    const r = legend.addStack();
    r.centerAlignContent();
    dot(r, p.color);
    r.addSpacer(5);
    addText(r, p.label, F.label, C.ink2);
    r.addSpacer();
    addText(r, kwh(p.value), F.value, C.ink);
  }
  card.addSpacer();
}

function batteryCard(parent, d, width, height) {
  const card = tile(parent, width, height);
  const bkw = d.battery_kw || 0;
  const head = card.addStack();
  head.centerAlignContent();
  addText(head, "Akku", F.title, C.ink);
  head.addSpacer();
  const flow = Math.abs(bkw) * 1000 < 10 ? "ruht" : `${bkw > 0 ? "↑" : "↓"} ${power(bkw)}`;
  addText(head, flow, F.label, C.ink2);
  card.addSpacer();

  const body = card.addStack();
  body.addSpacer();
  const dia = Math.min(height - 16 - 15 - 4, 70);
  const ring = body.addStack();
  ring.size = new Size(dia, dia);
  ring.backgroundImage = gaugeImage(dia, (d.battery_soc_pct || 0) / 100, S.batt, 9);
  ring.centerAlignContent();
  ring.addSpacer();
  addText(ring, pct(d.battery_soc_pct), F.big, C.ink);
  ring.addSpacer();
  body.addSpacer();
  card.addSpacer();
}

function hourlyCard(parent, d, width, height) {
  const card = tile(parent, width, height);
  const head = card.addStack();
  head.centerAlignContent();
  addText(head, "Heute je Stunde", F.title, C.ink);
  head.addSpacer();
  [
    ["PV", S.pv],
    ["Akku", S.batt],
    ["Netz", S.grid],
  ].forEach(([label, color], i) => {
    if (i) head.addSpacer(8);
    dot(head, color);
    head.addSpacer(4);
    addText(head, label, F.label, C.ink2);
  });
  card.addSpacer(4);

  const inner = width - 18;
  const GUTTER = 14;
  const chartW = inner - GUTTER;
  const chartH = Math.max(30, height - 16 - 15 - 4 - 3 - ROW_H);
  const row = card.addStack();
  const gutter = row.addStack();
  gutter.layoutVertically();
  gutter.size = new Size(GUTTER, chartH);
  // Generation is drawn above the line, consumption below it.
  addText(gutter, "↑", F.label, C.ink2, 1);
  gutter.addSpacer();
  addText(gutter, "↓", F.label, C.ink2, 1);
  const img = row.addImage(hourlyImage(chartW, chartH, d.hours));
  img.imageSize = new Size(chartW, chartH);

  card.addSpacer(3);
  const axis = card.addStack();
  axis.addSpacer(GUTTER);
  ["0", "6", "12", "18"].forEach((label, i) => {
    const cell = axis.addStack();
    cell.size = new Size(chartW / 4, 0);
    addText(cell, label, F.label, C.ink2, 1);
    cell.addSpacer();
    if (i === 3) addText(cell, "24", F.label, C.ink2, 1);
  });
}

// ------------------------------------------------------------ assemble
function header(widget, state) {
  const row = widget.addStack();
  row.centerAlignContent();
  addText(row, "Energie", F.head, C.ink);
  row.addSpacer();
  const f = freshness(state);
  addText(row, f.text, F.label, f.warn ? C.warn : C.ink2);
}

function build(family, state) {
  const widget = new ListWidget();
  widget.backgroundColor = C.bg;
  widget.refreshAfterDate = new Date(Date.now() + REFRESH_MIN * 60 * 1000);
  if (family !== "large" && family !== "extraLarge") {
    addText(widget, "Energie Grafik", F.title, C.ink);
    widget.addSpacer(4);
    const t = widget.addText("Für die Größe Groß (4x4) gebaut.");
    t.font = F.label;
    t.textColor = C.ink2;
    return widget;
  }
  if (!state) {
    addText(widget, "Keine Verbindung", F.title, C.ink);
    widget.addSpacer(4);
    const t = widget.addText("Pi nicht erreichbar. Tailscale an?");
    t.font = F.label;
    t.textColor = C.ink2;
    return widget;
  }
  widget.url = `${state.host}/`;

  const PAD = 12;
  const GAP = 8;
  widget.setPadding(PAD, PAD, PAD, PAD);
  const size = widgetSize();
  const W = size.width - 2 * PAD;
  const H = size.height - 2 * PAD;
  const d = state.data;

  header(widget, state);
  widget.addSpacer(GAP);
  const plan = planFlow(d, W);
  flowCard(widget, plan, W);
  widget.addSpacer(GAP);

  // The hourly chart gets what the others leave, and the middle row gives
  // way first when the flow card needs a second line of values.
  const left = H - 16 - 3 * GAP - plan.height;
  const midH = Math.min(100, Math.max(80, left - 100));
  const row = widget.addStack();
  row.spacing = GAP;
  const lw = Math.round((W - GAP) * 0.6);
  donutCard(row, d, lw, midH);
  batteryCard(row, d, W - GAP - lw, midH);
  widget.addSpacer(GAP);
  hourlyCard(widget, d, W, left - midH);
  return widget;
}

// ------------------------------------------------------------ self-update
// As in "Energie": at most once an hour, every time when opened in the app.
// A download is written only if it starts like this file, carries filled-in
// addresses and parses - a script that does not run cannot update itself
// back.
const UPDATE_EVERY_H = 1;
const MARKER = "// Energie Grafik - the Home Energy Manager as a chart widget";
const UPDATE_STAMP = "energie-grafik-update.txt";

async function selfUpdate(host, force) {
  try {
    const own = module.filename;
    const scripts = own.includes("Mobile Documents") ? FileManager.iCloud() : FileManager.local();
    const stamp = fm.joinPath(fm.documentsDirectory(), UPDATE_STAMP);
    if (!force && fm.fileExists(stamp)) {
      const last = Number(fm.readString(stamp)) || 0;
      if (Date.now() - last < UPDATE_EVERY_H * 3600 * 1000) return "skipped";
    }
    const req = new Request(`${host}${SCRIPT_PATH}`);
    req.timeoutInterval = TIMEOUT_S;
    const code = await req.loadString();
    if (!req.response || req.response.statusCode !== 200) return "failed";
    fm.writeString(stamp, String(Date.now()));
    if (!code.startsWith(MARKER) || !/const HOSTS = \["http/.test(code)) return "failed";
    const norm = (s) => s.replace(/\r\n/g, "\n");
    if (norm(code) === norm(scripts.readString(own))) return "current";
    new (Object.getPrototypeOf(async function () {}).constructor)(code);
    scripts.writeString(own, code);
    return "updated";
  } catch (e) {
    return "failed";
  }
}

const UPDATE_TEXT = {
  updated: "Neue Version geladen, aktiv ab dem nächsten Aufruf.",
  current: "Aktuelle Version.",
  failed: "Update-Prüfung fehlgeschlagen.",
  offline: "Pi nicht erreichbar, keine Update-Prüfung.",
  skipped: "",
};

const state = await load();
const inApp = !config.runsInWidget;
const update = state && !state.offline ? await selfUpdate(state.host, inApp) : "offline";

if (!inApp) {
  Script.setWidget(build(config.widgetFamily || "large", state));
} else {
  const note = new Alert();
  note.title = "Energie Grafik";
  note.message = UPDATE_TEXT[update] || "";
  note.addAction("Vorschau");
  await note.presentAlert();
  await build("large", state).presentLarge();
}
Script.complete();

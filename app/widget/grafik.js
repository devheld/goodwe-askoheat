// Energie Grafik - the Home Energy Manager as a chart widget (Scriptable app).
//
// The companion to "Energie": the same readings, drawn instead of listed.
// Two layouts from this one file, chosen by the Pi when it serves it:
//
//   4x4  one large widget ("Energie Grafik")
//   4x6  a large widget with a medium one placed directly under it
//        ("Energie 4x6") - iOS has no 4x6 size, so the two together are
//        laid out as one. The same script goes into both; each draws its
//        half by the size it was given.
//
// Charts are pictures and do not switch with the phone's appearance, and
// iOS does not tell a widget which appearance is on. So the marks take the
// light or the dark palette by the sun: with appearance set to automatic,
// iOS switches at sunset and sunrise. The widget's parameter "hell" or
// "dunkel" overrides that for a phone on a fixed appearance. Texts are
// widget texts and switch by themselves either way.
//
// Served by the Pi with HOSTS filled in, and updates itself from there.

const HOSTS = /*__HOSTS__*/[];
const LAYOUT = /*__LAYOUT__*/"4x4";
const SCRIPT_PATH = /*__SCRIPT__*/"/widget/grafik.js";
const DATA_PATH = "/api/widget/graph";
const TIMEOUT_S = 8;
const STALE_MIN = 10;
const REFRESH_MIN = 5;
const CACHE_FILE = "energie-grafik-cache.json";
const TITLE = { "4x6": "Energie 4x6", "2x2": "Energie StandBy" }[LAYOUT] || "Energie Grafik";
const LOCATION_FILE = "energie-grafik-location.json";

const fm = FileManager.local();
const inApp = !config.runsInWidget;

// ------------------------------------------------------------ light or dark
// Where the sun is, after the NOAA low-precision formulas: good to a
// fraction of a degree, which is a minute or two at sunset.
function sunElevation(date, lat, lon) {
  const rad = Math.PI / 180;
  const d = date.getTime() / 86400000 + 2440587.5 - 2451545.0;
  const g = (357.529 + 0.98560028 * d) % 360;
  const q = (280.459 + 0.98564736 * d) % 360;
  const l = q + 1.915 * Math.sin(g * rad) + 0.02 * Math.sin(2 * g * rad);
  const e = 23.439 - 0.00000036 * d;
  const ra = Math.atan2(Math.cos(e * rad) * Math.sin(l * rad), Math.cos(l * rad)) / rad;
  const dec = Math.asin(Math.sin(e * rad) * Math.sin(l * rad)) / rad;
  const gmst = (18.697374558 + 24.06570982441908 * d) % 24;
  const hourAngle = gmst * 15 + lon - ra;
  return (
    Math.asin(
      Math.sin(lat * rad) * Math.sin(dec * rad) +
        Math.cos(lat * rad) * Math.cos(dec * rad) * Math.cos(hourAngle * rad)
    ) / rad
  );
}

// The phone's position, asked for only when the script runs in the app
// (where iOS can ask for permission) and remembered; until then the middle
// of Switzerland, which is within a quarter of an hour of any sunset there.
async function homeLocation() {
  const path = fm.joinPath(fm.documentsDirectory(), LOCATION_FILE);
  if (inApp) {
    try {
      Location.setAccuracyToThreeKilometers();
      const here = await Location.current();
      fm.writeString(path, JSON.stringify({ latitude: here.latitude, longitude: here.longitude }));
      return here;
    } catch (e) {
      // No permission: the remembered or the default position.
    }
  }
  try {
    if (fm.fileExists(path)) return JSON.parse(fm.readString(path));
  } catch (e) {
    // fall through
  }
  return { latitude: 46.8, longitude: 8.2 };
}

async function darkAppearance() {
  const p = String(config.widgetParameter || "").toLowerCase();
  if (p.includes("dunkel")) return true;
  if (p.includes("hell")) return false;
  const here = await homeLocation(); // in the app this also remembers it
  // In the app the question can simply be asked.
  if (inApp) return Device.isUsingDarkAppearance();
  return sunElevation(new Date(), here.latitude, here.longitude) < -0.833;
}

// ------------------------------------------------------------ colours
const dyn = (light, dark) => Color.dynamic(new Color(light), new Color(dark));
const C = {
  bg: dyn("#ffffff", "#1c1c1e"),
  tile: dyn("#f2f2f7", "#2c2c2e"),
  ink: dyn("#1c1c1e", "#f2f2f7"),
  ink2: dyn("#6e6e73", "#98989f"),
  warn: dyn("#b35c00", "#ff9f0a"),
};

// The marks, one palette per appearance, each validated on its own tile
// colour in the groups the charts show together: sources (PV, battery,
// grid) and sinks (house, wallbox, heat pump, AskoHeat, battery, export:
// all pairs among the four loads, neighbours in the bar). The battery
// keeps its blue in both groups because it is the same thing.
const PALETTES = {
  light: {
    pv: "#e8860b", batt: "#2a78d6", grid: "#d6458a",
    house: "#8e8e93", wb: "#7a3fb5", hp: "#b5306b", exp: "#008300",
    asko: "#d4a017",
  },
  dark: {
    pv: "#c86e05", batt: "#3f8ee8", grid: "#cf4f8c",
    house: "#9a9aa0", wb: "#8d5bd8", hp: "#c2416f", exp: "#2fa84f",
    asko: "#c88300",
  },
};
let S = null; // set once the appearance is known

function usePalette(dark) {
  const p = PALETTES[dark ? "dark" : "light"];
  S = {};
  for (const k of Object.keys(p)) S[k] = new Color(p[k]);
  S.track = new Color("#8e8e93", dark ? 0.3 : 0.22);
  S.axis = new Color("#8e8e93", 0.7);
  S.socFill = new Color(p.batt, 0.18);
  // Text drawn into chart images: dynamic colours don't resolve there.
  S.ink = new Color(dark ? "#f2f2f7" : "#1c1c1e");
  S.ink2 = new Color(dark ? "#98989f" : "#6e6e73");
  S.tile = new Color(dark ? "#2c2c2e" : "#f2f2f7");
}

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

const has = (v) => v !== null && v !== undefined && !isNaN(v);
const kwh = (v) => (!has(v) ? "–" : v >= 100 ? `${Math.round(v)}` : v.toFixed(1));
const pct = (v) => (!has(v) ? "–" : `${Math.round(v)} %`);
const sum = (parts) => parts.reduce((a, p) => a + Math.max(0, p.value || 0), 0);

function batteryFlow(bkw) {
  return Math.abs(bkw || 0) * 1000 < 10 ? "ruht" : `${bkw > 0 ? "↑" : "↓"} ${power(bkw)}`;
}

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

// Where the day's PV went: export first, then the battery, and the house
// had the rest.
function pvUse(pv, exported, charged) {
  const gen = Math.max(0, pv || 0);
  const exp = Math.min(Math.max(0, exported || 0), gen);
  const batt = Math.min(Math.max(0, charged || 0), gen - exp);
  return { house: gen - exp - batt, batt, exp };
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
    ctx.fillRect(new Rect(0, Math.round(h / 2), w, 1));
    return { image: ctx.getImage(), base: Math.round(h / 2), gen: false };
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
  return { image: ctx.getImage(), base, gen: maxUp > 0 };
}

const LABEL_H = 14; // one line of 11 pt text drawn into an image

function drawLabel(ctx, str, x, y, w, align, color, font) {
  ctx.setFont(font || F.label);
  ctx.setTextColor(color || S.ink2);
  if (align === "right") ctx.setTextAlignedRight();
  else ctx.setTextAlignedLeft();
  ctx.drawTextInRect(str, new Rect(x, y, w, LABEL_H));
}

// The words beside the hourly chart, on either side of its line.
function hourlyGutterImage(w, h, base, gen) {
  const ctx = canvas(w, h);
  const inner = w - 4;
  let below = Math.min(h - LABEL_H, base + 1);
  if (gen) {
    const above = Math.max(0, Math.min(base - LABEL_H, below - LABEL_H));
    drawLabel(ctx, "Erzeugt", 0, above, inner, "right");
    below = Math.max(below, above + LABEL_H);
  }
  drawLabel(ctx, "Verbrauch", 0, below, inner, "right");
  return ctx.getImage();
}

// The scale of the battery strip: 100 % at its top, 0 % at its foot.
function socGutterImage(w, h) {
  const ctx = canvas(w, h);
  drawLabel(ctx, "100 %", 0, -1, w - 4, "right");
  drawLabel(ctx, "0 %", 0, h - LABEL_H + 1, w - 4, "right");
  return ctx.getImage();
}

// The battery's charge level through the day, 0 to 100 %, on the same
// hours as the chart above it: a line with a light fill under it.
function socImage(w, h, hours) {
  const ctx = canvas(w, h);
  const slot = w / 24;
  ctx.setFillColor(S.track);
  ctx.fillRect(new Rect(0, h - 1, w, 1));
  ctx.fillRect(new Rect(0, 0, w, 1));
  const last = (hours || []).filter((x) => has(x.soc)).pop();
  const pts = (hours || [])
    .filter((x) => has(x.soc))
    .map((x) => new Point((x.h + 0.5) * slot, 1 + (h - 2) * (1 - Math.max(0, Math.min(100, x.soc)) / 100)));
  if (pts.length < 2) return ctx.getImage();
  fillPolygon(ctx, [new Point(pts[0].x, h), ...pts, new Point(pts[pts.length - 1].x, h)], S.socFill);
  const line = new Path();
  line.move(pts[0]);
  for (let i = 1; i < pts.length; i++) line.addLine(pts[i]);
  ctx.addPath(line);
  ctx.setStrokeColor(S.batt);
  ctx.setLineWidth(2);
  ctx.strokePath();

  // Where it stands now: a dot on the end of the line and the number.
  const end = pts[pts.length - 1];
  ctx.setFillColor(S.batt);
  ctx.fillEllipse(new Rect(end.x - 3.5, end.y - 3.5, 7, 7));
  const text = `Akku ${Math.round(last.soc)} %`;
  const tw = 70;
  const y = Math.max(0, Math.min(h - LABEL_H, end.y - LABEL_H / 2));
  // Late in the day there is no room after the line: put it before,
  // above the line, or under it when the line runs along the top. A
  // patch of tile colour keeps the line from running through it.
  const after = end.x + 6 + tw <= w;
  const tx = after ? end.x + 6 : end.x - 6 - tw;
  const ty = after ? y : end.y - 2 - LABEL_H >= 0 ? end.y - 2 - LABEL_H : Math.min(h - LABEL_H, end.y + 3);
  if (!after) {
    const halo = new Path();
    halo.addRoundedRect(new Rect(tx + tw - 58, ty, 58, LABEL_H), 4, 4);
    ctx.addPath(halo);
    ctx.setFillColor(S.tile);
    ctx.fillPath();
  }
  drawLabel(ctx, text, tx, ty, tw, after ? "left" : "right", S.ink, F.value);
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

const TILE_PAD_H = 9;

function tile(parent, width, height) {
  const t = parent.addStack();
  t.layoutVertically();
  t.size = new Size(width, height || 0);
  t.backgroundColor = C.tile;
  t.cornerRadius = 12;
  t.setPadding(8, TILE_PAD_H, 8, TILE_PAD_H);
  return t;
}

function headRow(card, title, note, noteColor) {
  const head = card.addStack();
  head.centerAlignContent();
  addText(head, title, F.title, C.ink);
  head.addSpacer();
  if (note) addText(head, note, F.label, noteColor || C.ink2);
  return head;
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

const SIZES = [
  // screen width, small, medium/large width, large height
  [428, 170, 364, 382],
  [414, 169, 360, 379],
  [402, 162, 348, 362],
  [390, 158, 338, 354],
  [375, 155, 329, 345],
  [0, 141, 292, 311],
];

function widgetSize(family) {
  const sw = Device.screenSize().width;
  const [, small, wide, tall] = SIZES.find(([min]) => sw >= min);
  if (family === "small") return new Size(small, small);
  return family === "medium" ? new Size(wide, small) : new Size(wide, tall);
}

// ------------------------------------------------------------ the cards
const LABEL_W = 46;
const BAR_H = 10;

function flowParts(d) {
  const hp = Math.max(0, d.heatpump_kw || 0);
  const wb = Math.max(0, d.wallbox_kw || 0);
  const asko = Math.max(0, (d.askoheat_w || 0) / 1000);
  const battery = d.battery_kw || 0;
  return {
    from: [
      { label: "PV", value: Math.max(0, d.pv_kw || 0), color: S.pv },
      { label: "Akku", value: Math.max(0, -battery), color: S.batt },
      { label: "Netz", value: Math.max(0, d.grid_import_kw || 0), color: S.grid },
    ],
    // The house figure is the rest once the metered loads are off it.
    to: [
      { label: "Haus", value: Math.max(0, (d.consumption_kw || 0) - hp - wb - asko), color: S.house },
      { label: "Auto", value: wb, color: S.wb },
      { label: "WP", value: hp, color: S.hp },
      { label: "Asko", value: asko, color: S.asko },
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

// Laid out before it is drawn, so its height is known and the charts under
// it can have what is left.
function planFlow(d, width) {
  const inner = width - 2 * TILE_PAD_H;
  const parts = flowParts(d);
  const fromRows = chipRows(chipsOf(parts.from), inner - LABEL_W);
  const toRows = chipRows(chipsOf(parts.to), inner - LABEL_W);
  const group = (rows) => BAR_H + 4 + Math.max(1, rows.length) * ROW_H;
  const height = 16 + 15 + 6 + group(fromRows) + 7 + group(toRows);
  return { parts, fromRows, toRows, inner, height };
}

function flowCard(parent, plan, width) {
  const card = tile(parent, width, plan.height);
  headRow(card, "Jetzt", "kW");
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

// A donut with what its parts add up to in the middle and a legend beside
// it. A small ring is thinner and its number shorter, so the number stays
// inside it.
function donutCard(parent, spec, width, height, maxDia) {
  const card = tile(parent, width, height);
  headRow(card, spec.title, spec.note);
  card.addSpacer();

  const body = card.addStack();
  body.centerAlignContent();
  const dia = Math.min(height - 16 - 15 - 4, maxDia);
  const small = dia < 58;
  const ring = body.addStack();
  ring.size = new Size(dia, dia);
  ring.backgroundImage = donutImage(dia, spec.parts, small ? 7 : 9);
  ring.layoutVertically();
  ring.addSpacer();
  const l1 = ring.addStack();
  l1.addSpacer();
  addText(l1, small && has(spec.total) ? `${Math.round(spec.total)}` : kwh(spec.total), F.big, C.ink);
  l1.addSpacer();
  if (!small) {
    const l2 = ring.addStack();
    l2.addSpacer();
    addText(l2, "kWh", F.label, C.ink2);
    l2.addSpacer();
  }
  ring.addSpacer();

  const gap = 8;
  body.addSpacer(gap);
  const legend = body.addStack();
  legend.layoutVertically();
  legend.spacing = spec.parts.length > 3 ? 2 : 4;
  // A fixed width, so the spacer in each row has room to push the number
  // to the right edge.
  legend.size = new Size(width - 2 * TILE_PAD_H - dia - gap, 0);
  for (const p of spec.parts) {
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

// Today's consumption by what used it. Where it came from is in the hourly
// chart. Loads that used nothing today are left out of ring and legend.
function consumptionSpec(d, title) {
  const t = d.today || {};
  return {
    title,
    note: `${pct(t.autarky_pct)} autark`,
    total: t.consumption_kwh,
    parts: [
      { label: "Haus", value: t.other_kwh, color: S.house },
      { label: "Auto", value: t.wallbox_kwh, color: S.wb },
      { label: "WP", value: t.heatpump_kwh, color: S.hp },
      { label: "Asko", value: t.askoheat_kwh, color: S.asko },
    ].filter((p) => (p.value || 0) >= 0.05),
  };
}

function pvSpec(d) {
  const t = d.today || {};
  const u = pvUse(t.pv_kwh, t.export_kwh, t.battery_charge_kwh);
  return {
    title: "PV",
    note: `${pct(t.self_consumption_pct)} genutzt`,
    total: t.pv_kwh,
    parts: [
      { label: "Haus", value: u.house, color: S.house },
      { label: "Akku", value: u.batt, color: S.batt },
      { label: "Einsp.", value: u.exp, color: S.exp },
    ],
  };
}

function batteryCard(parent, d, width, height) {
  const card = tile(parent, width, height);
  headRow(card, "Akku", batteryFlow(d.battery_kw));
  card.addSpacer();
  const body = card.addStack();
  body.addSpacer();
  const dia = Math.min(height - 16 - 15 - 4, 70);
  addGauge(body, d, dia);
  body.addSpacer();
  card.addSpacer();
}

function addGauge(parent, d, dia) {
  const small = dia < 58;
  const ring = parent.addStack();
  ring.size = new Size(dia, dia);
  ring.backgroundImage = gaugeImage(dia, (d.battery_soc_pct || 0) / 100, S.batt, small ? 7 : 9);
  ring.centerAlignContent();
  ring.addSpacer();
  const soc = d.battery_soc_pct;
  addText(ring, small && has(soc) ? `${Math.round(soc)}%` : pct(soc), F.big, C.ink);
  ring.addSpacer();
}

// The 4x6 top's battery card: the ring, and beside it what the battery is
// doing and what it has moved today.
function batteryWideCard(parent, d, width, height) {
  const card = tile(parent, width, height);
  card.layoutHorizontally();
  card.centerAlignContent();
  const dia = Math.min(height - 16, 64);
  addGauge(card, d, dia);
  card.addSpacer(14);
  const info = card.addStack();
  info.layoutVertically();
  info.spacing = 3;
  info.size = new Size(width - 2 * TILE_PAD_H - dia - 14, 0);
  const t = d.today || {};
  const row = (label, value, font, color) => {
    const r = info.addStack();
    r.centerAlignContent();
    addText(r, label, font, color);
    r.addSpacer();
    addText(r, value, F.value, C.ink);
  };
  row("Akku", batteryFlow(d.battery_kw), F.title, C.ink);
  row("Heute geladen", `${kwh(t.battery_charge_kwh)} kWh`, F.label, C.ink2);
  row("Heute entladen", `${kwh(t.battery_discharge_kwh)} kWh`, F.label, C.ink2);
}

const GUTTER = 62; // room for "Verbrauch" beside the hourly chart

function addLegend(row, items) {
  items.forEach(([label, color], i) => {
    if (i) row.addSpacer(8);
    dot(row, color);
    row.addSpacer(4);
    addText(row, label, F.label, C.ink2);
  });
}

function addLabeledChart(card, gutterImage, image, w, h) {
  const row = card.addStack();
  const g = row.addImage(gutterImage);
  g.imageSize = new Size(GUTTER, h);
  const img = row.addImage(image);
  img.imageSize = new Size(w, h);
}

function addHourAxis(card, chartW) {
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

// Today hour by hour; with withSoc, the battery's charge level as a strip
// of its own under it - a percentage, so not on the kWh scale.
function hourlyCard(parent, d, width, height, withSoc) {
  const card = tile(parent, width, height);
  const head = card.addStack();
  head.centerAlignContent();
  addText(head, "Heute je Stunde", F.title, C.ink);
  head.addSpacer();
  addLegend(head, [
    ["PV", S.pv],
    ["Akku", S.batt],
    ["Netz", S.grid],
  ]);
  card.addSpacer(4);

  const chartW = width - 2 * TILE_PAD_H - GUTTER;
  const SOC_H = 32;
  const socBlock = withSoc ? SOC_H + 5 : 0;
  const chartH = Math.max(30, height - 16 - 15 - 4 - 3 - ROW_H - socBlock);
  // Generation is drawn above the line, consumption below it.
  const hourly = hourlyImage(chartW, chartH, d.hours);
  addLabeledChart(card, hourlyGutterImage(GUTTER, chartH, hourly.base, hourly.gen), hourly.image, chartW, chartH);
  if (withSoc) {
    card.addSpacer(5);
    addLabeledChart(card, socGutterImage(GUTTER, SOC_H), socImage(chartW, SOC_H, d.hours), chartW, SOC_H);
  }
  card.addSpacer(3);
  addHourAxis(card, chartW);
}

// ------------------------------------------------------------ StandBy
// The 2x2 for StandBy, the phone on its side on the charger: iOS shows a
// small widget there at about twice its size, on black, red at night. So
// it is one picture, drawn at twice the size so it stays sharp.
//
// A 24-hour dial with noon at the top: the sun's path runs from the left
// (morning) over the top to the right (evening). Each hour's PV grows out
// of the ring, its consumption into it, coloured by where it came from -
// the hourly chart bent into a circle, on one scale. The ring is brighter
// up to now and a dot marks the moment. Inside: the battery as a ring and
// the one number that matters now. Below: today's yield and the battery.
const SB = 2;

function hourAngle(h) {
  return ((h - 12) / 24) * 2 * Math.PI;
}

function sbText(ctx, str, x, y, w, h, font, color, align) {
  ctx.setFont(font);
  ctx.setTextColor(color);
  if (align === "right") ctx.setTextAlignedRight();
  else if (align === "center") ctx.setTextAlignedCenter();
  else ctx.setTextAlignedLeft();
  ctx.drawTextInRect(str, new Rect(x * SB, y * SB, w * SB, h * SB));
}

function bigNumber(v) {
  const a = Math.abs(v || 0);
  return a < 0.01 ? "0" : a < 10 ? a.toFixed(a < 1 ? 2 : 1) : String(Math.round(a));
}

function standbyImage(d, state, size) {
  const ctx = new DrawContext();
  ctx.size = new Size(size * SB, size * SB);
  ctx.opaque = false;
  ctx.respectScreenScale = true;
  const c = (size / 2) * SB;
  const r = (f) => size * f * SB;
  const rBase = r(0.33);
  const rOut = r(0.485);
  const rIn = r(0.24);
  const battOut = r(0.214);
  const battIn = battOut - 3.5 * SB;
  const hw = (1.25 * Math.PI) / 180; // half the gap between two hours

  // The hours: generation outward, consumption inward by origin.
  const rows = (d.hours || []).map((x) => ({
    h: x.h,
    gen: Math.max(0, x.pv || 0),
    ...origin(x.consumption, x.import, x.discharge),
  }));
  const maxUp = Math.max(0, ...rows.map((x) => x.gen));
  const maxDown = Math.max(0, ...rows.map((x) => x.pv + x.batt + x.grid));
  const roomUp = rOut - rBase - SB;
  const roomDown = rBase - rIn - SB;
  const k = Math.min(maxUp > 0 ? roomUp / maxUp : Infinity, maxDown > 0 ? roomDown / maxDown : Infinity);
  if (isFinite(k)) {
    for (const x of rows) {
      const a0 = hourAngle(x.h) + hw;
      const a1 = hourAngle(x.h + 1) - hw;
      const up = x.gen * k;
      if (up >= 0.5 * SB) ringSegment(ctx, c, c, rBase + SB + up, rBase + SB, a0, a1, S.pv);
      let from = rBase - SB;
      for (const [v, color] of [[x.pv, S.pv], [x.batt, S.batt], [x.grid, S.grid]]) {
        const span = v * k;
        if (span < 0.5 * SB) continue;
        ringSegment(ctx, c, c, from, from - span + (span > 3 * SB ? SB : 0), a0, a1, color);
        from -= span;
      }
    }
  }

  // The ring: the day so far brighter, then the dot for now.
  const now = new Date();
  const nowH = now.getHours() + now.getMinutes() / 60;
  ringSegment(ctx, c, c, rBase + 0.6 * SB, rBase - 0.6 * SB, 0, 2 * Math.PI, S.track);
  ringSegment(ctx, c, c, rBase + 0.6 * SB, rBase - 0.6 * SB, hourAngle(0), hourAngle(nowH), S.axis);
  const na = hourAngle(nowH);
  const dot = 3.5 * SB;
  ctx.setFillColor(S.ink);
  ctx.fillEllipse(new Rect(c + rBase * Math.sin(na) - dot, c - rBase * Math.cos(na) - dot, 2 * dot, 2 * dot));

  // The battery.
  const soc = Math.max(0, Math.min(100, d.battery_soc_pct || 0)) / 100;
  ringSegment(ctx, c, c, battOut, battIn, 0, 2 * Math.PI, S.track);
  ringSegment(ctx, c, c, battOut, battIn, 0, soc * 2 * Math.PI, S.batt);

  // The middle: the sun while it delivers, otherwise what the house draws.
  const cx = size / 2;
  const sun = (d.pv_kw || 0) >= 0.05;
  const f = freshness(state);
  const big = Font.boldRoundedSystemFont(22 * SB);
  const small = Font.mediumSystemFont(10 * SB);
  const value = Font.semiboldRoundedSystemFont(14 * SB);
  sbText(ctx, sun ? "Sonne" : "Bedarf", cx - 25, cx - 24, 50, 13, small, S.ink2, "center");
  sbText(ctx, bigNumber(sun ? d.pv_kw : d.consumption_kw), cx - 30, cx - 14, 60, 28, big, S.ink, "center");
  sbText(ctx, f.warn ? clock(parseTs(d.timestamp)) : "kW", cx - 25, cx + 12, 50, 13, small, f.warn ? new Color("#ff9f0a") : S.ink2, "center");

  // The two lower corners, which the dial leaves free (no sun at night):
  // today's yield and the battery. Inset for the widget's rounded corners.
  const t = d.today || {};
  const corner = (x, v, label, align) => {
    sbText(ctx, v, x, size - 39, 40, 17, value, S.ink, align);
    sbText(ctx, label, x, size - 23, 40, 13, small, S.ink2, align);
  };
  corner(12, kwh(t.pv_kwh), "kWh PV", "left");
  corner(size - 52, pct(d.battery_soc_pct).replace(" ", ""), "Akku", "right");
  return ctx.getImage();
}

function buildStandby(widget, state, size) {
  widget.setPadding(0, 0, 0, 0);
  widget.backgroundColor = new Color("#000000");
  // A content image, not a background: StandBy may drop backgrounds.
  const img = widget.addImage(standbyImage(state.data, state, size.width));
  img.imageSize = size;
  img.centerAlignImage();
}

// ------------------------------------------------------------ layouts
const PAD = 12;
const GAP = 8;

// The balcony unit as a plain status: the inverter cannot see it, so it
// is in none of the charts.
function balconyStatus(row, d) {
  const now = d.balcony_kw;
  const today = (d.today || {}).balcony_kwh;
  if (!has(now) && !has(today)) return;
  row.addSpacer();
  const sym = SFSymbol.named("sun.max.fill");
  const icon = row.addImage(sym.image);
  icon.imageSize = new Size(12, 12);
  icon.tintColor = S.pv;
  row.addSpacer(4);
  addText(row, "Balkon", F.label, C.ink2);
  row.addSpacer(4);
  const parts = [];
  if (has(now)) parts.push(power(now));
  if (has(today)) parts.push(`${today.toFixed(2)} kWh`);
  addText(row, parts.join(" · "), F.value, C.ink);
}

function header(widget, state) {
  const row = widget.addStack();
  row.centerAlignContent();
  addText(row, "Energie", F.head, C.ink);
  balconyStatus(row, state.data);
  row.addSpacer();
  const f = freshness(state);
  addText(row, f.text, F.label, f.warn ? C.warn : C.ink2);
}

function build4x4(widget, d, state, W, H) {
  header(widget, state);
  widget.addSpacer(GAP);
  const plan = planFlow(d, W);
  flowCard(widget, plan, W);
  widget.addSpacer(GAP);
  // The hourly chart gets what the others leave, and the middle row gives
  // way first when the flow card needs a second line of values.
  const left = H - 16 - 3 * GAP - plan.height;
  const midH = Math.min(100, Math.max(94, left - 90));
  const row = widget.addStack();
  row.spacing = GAP;
  const lw = Math.round((W - GAP) * 0.6);
  donutCard(row, consumptionSpec(d, "Heute"), lw, midH, 70);
  batteryCard(row, d, W - GAP - lw, midH);
  widget.addSpacer(GAP);
  hourlyCard(widget, d, W, left - midH, false);
}

// 4x6, the large half: now, today's two donuts, the battery.
function buildTop(widget, d, state, W, H) {
  header(widget, state);
  widget.addSpacer(GAP);
  const plan = planFlow(d, W);
  flowCard(widget, plan, W);
  widget.addSpacer(GAP);
  const left = H - 16 - 3 * GAP - plan.height;
  const donutH = Math.min(108, Math.max(84, left - 80));
  const row = widget.addStack();
  row.spacing = GAP;
  const half = (W - GAP) / 2;
  donutCard(row, consumptionSpec(d, "Verbrauch"), half, donutH, 56);
  donutCard(row, pvSpec(d), half, donutH, 56);
  widget.addSpacer(GAP);
  batteryWideCard(widget, d, W, left - donutH);
}

// 4x6, the medium half: today hour by hour with the battery's level under
// it, the whole widget for the one chart that needs width.
function buildBottom(widget, d, state, W, H) {
  hourlyCard(widget, d, W, H, true);
}

function build(family, state) {
  const widget = new ListWidget();
  widget.backgroundColor = C.bg;
  widget.refreshAfterDate = new Date(Date.now() + REFRESH_MIN * 60 * 1000);
  const fits =
    LAYOUT === "4x6"
      ? family === "large" || family === "medium"
      : LAYOUT === "2x2"
        ? family === "small"
        : family === "large";
  if (!fits) {
    addText(widget, TITLE, F.title, C.ink);
    widget.addSpacer(4);
    const t = widget.addText(
      LAYOUT === "4x6"
        ? "Groß (4x4) oben, Mittel (4x2) darunter."
        : LAYOUT === "2x2"
          ? "Für die Größe Klein (2x2) gebaut."
          : "Für die Größe Groß (4x4) gebaut."
    );
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
  const size = widgetSize(family);
  if (LAYOUT === "2x2") {
    buildStandby(widget, state, size);
    return widget;
  }
  widget.setPadding(PAD, PAD, PAD, PAD);
  const W = size.width - 2 * PAD;
  const H = size.height - 2 * PAD;
  if (LAYOUT !== "4x6") build4x4(widget, state.data, state, W, H);
  else if (family === "large") buildTop(widget, state.data, state, W, H);
  else buildBottom(widget, state.data, state, W, H);
  return widget;
}

// ------------------------------------------------------------ self-update
// As in "Energie": at most once an hour, every time when opened in the app.
// A download is written only if it starts like this file, carries filled-in
// addresses and parses - a script that does not run cannot update itself
// back.
const UPDATE_EVERY_H = 1;
const MARKER = "// Energie Grafik - the Home Energy Manager as a chart widget";
const UPDATE_STAMP = `energie-grafik-${LAYOUT}-update.txt`;

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

// StandBy is always dark; "hell" as parameter for a light home screen.
usePalette(
  LAYOUT === "2x2" ? !String(config.widgetParameter || "").toLowerCase().includes("hell") : await darkAppearance()
);
const state = await load();
const update = state && !state.offline ? await selfUpdate(state.host, inApp) : "offline";

if (!inApp) {
  Script.setWidget(build(config.widgetFamily || "large", state));
} else {
  const note = new Alert();
  note.title = TITLE;
  note.message = UPDATE_TEXT[update] || "";
  if (LAYOUT === "2x2") {
    note.addAction("Vorschau Klein");
    note.addCancelAction("Fertig");
    if ((await note.presentAlert()) === 0) await build("small", state).presentSmall();
  } else {
    note.addAction("Vorschau Groß");
    if (LAYOUT === "4x6") note.addAction("Vorschau Mittel");
    note.addCancelAction("Fertig");
    const choice = await note.presentAlert();
    if (choice === 0) await build("large", state).presentLarge();
    if (choice === 1) await build("medium", state).presentMedium();
  }
}
Script.complete();

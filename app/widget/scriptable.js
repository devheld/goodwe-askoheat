// Energie - the Home Energy Manager as an iPhone widget (Scriptable app).
//
// One script, every size: small (2x2), medium (4x2), large (4x4), and the
// three lock screen shapes. The layout follows the size the widget was
// placed at. Tapping it opens the dashboard.
//
// Served by the Pi at /widget/scriptable.js, which fills in HOSTS below:
// WIDGET_HOSTS from .env, plus the address the phone downloaded it from.
// It updates itself from there (see self-update at the end).
// All of them are tried at once and the first answer wins, so the widget
// works over the VPN away from home and on the LAN with the VPN off.
//
// When nothing answers it shows the last reading it got, marked offline,
// rather than an empty box.

const HOSTS = /*__HOSTS__*/[];
const TIMEOUT_S = 6;
const STALE_MIN = 10; // older than this, the Pi has stopped measuring
const REFRESH_MIN = 5; // a request; iOS decides when it actually happens
const CACHE_FILE = "energie-widget-cache.json";

// ------------------------------------------------------------ colours
// The dashboard's series colours, light and dark, on the icons only.
// Numbers and labels stay in text colours: the icon and the word carry
// the identity, the colour only repeats it.
const dyn = (light, dark) => Color.dynamic(new Color(light), new Color(dark));
const C = {
  bg: dyn("#ffffff", "#1c1c1e"),
  tile: dyn("#f2f2f7", "#2c2c2e"),
  ink: dyn("#1c1c1e", "#f2f2f7"),
  ink2: dyn("#6e6e73", "#98989f"),
  dim: dyn("#aeaeb2", "#636366"),
  warn: dyn("#b35c00", "#ff9f0a"),
  pv: dyn("#2a78d6", "#3987e5"),
  // The sun on the PV and balcony icons: a bright orange, yellower than
  // the house icon's orange-red so the two do not read as one.
  sun: dyn("#ff9500", "#ff9f0a"),
  cons: dyn("#eb6834", "#d95926"),
  grid: dyn("#e87ba4", "#d55181"),
  export: dyn("#008300", "#30a14e"),
  soc: dyn("#0f8a93", "#2fb3bd"),
  hp: dyn("#b5306b", "#d9538a"),
  wb: dyn("#7a3fb5", "#b07de0"),
};

// One size per kind of text, the same on every tile and every widget
// size. iOS shrinks a text only when it would not fit otherwise (the
// minimum scale factors below), so a value is 20 pt unless it cannot be.
const SIZE = { value: 20, label: 12, sub: 11, smallValue: 14 };

const F = {
  value: (s) => Font.boldRoundedSystemFont(s),
  semi: (s) => Font.semiboldRoundedSystemFont(s),
  label: (s) => Font.mediumSystemFont(s),
};

// ------------------------------------------------------------ formatting
const isZero = (kw) =>
  kw === null || kw === undefined || isNaN(kw) || Math.abs(kw) * 1000 < 10;

function power(kw) {
  if (kw === null || kw === undefined || isNaN(kw)) return "–";
  const w = Math.abs(kw) * 1000;
  if (w < 10) return "0 W";
  if (w < 1000) return `${Math.round(w)} W`;
  return `${(w / 1000).toFixed(1)} kW`;
}

const pct = (v) => (v === null || v === undefined ? "–" : `${Math.round(v)} %`);
const num = (v) =>
  v === null || v === undefined ? "–" : v < 10 ? v.toFixed(1) : `${Math.round(v)}`;

function parseTs(ts) {
  // Python writes microseconds; JavaScriptCore wants at most milliseconds.
  return new Date(String(ts).replace(/(\.\d{3})\d+/, "$1"));
}

function clock(date) {
  const df = new DateFormatter();
  df.dateFormat = "HH:mm";
  return df.string(date);
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
  const req = new Request(`${host}/api/widget`);
  req.timeoutInterval = TIMEOUT_S;
  const data = await req.loadJSON();
  if (!req.response || req.response.statusCode !== 200 || !data || !data.timestamp) {
    throw new Error("bad answer");
  }
  return { host, data };
}

const fm = FileManager.local();
const cachePath = fm.joinPath(fm.documentsDirectory(), CACHE_FILE);

function readCache() {
  try {
    return fm.fileExists(cachePath) ? JSON.parse(fm.readString(cachePath)) : null;
  } catch (e) {
    return null;
  }
}

function writeCache(state) {
  try {
    fm.writeString(cachePath, JSON.stringify({ host: state.host, data: state.data }));
  } catch (e) {
    // A widget that cannot write its cache still shows the live reading.
  }
}

async function load() {
  try {
    const got = await firstSuccess(HOSTS.map(fetchFrom));
    writeCache(got);
    return { ...got, offline: false };
  } catch (e) {
    const cached = readCache();
    return cached ? { ...cached, offline: true } : null;
  }
}

// ------------------------------------------------------------ what is shown
function batterySymbol(soc, charging) {
  if (charging) return "battery.100.bolt";
  if (soc >= 88) return "battery.100";
  if (soc >= 63) return "battery.75";
  if (soc >= 38) return "battery.50";
  if (soc >= 13) return "battery.25";
  return "battery.0";
}

function metrics(d) {
  const charging = d.battery_kw > 0.01;
  const resting = isZero(d.battery_kw);
  return [
    {
      label: "PV", short: "PV", sym: "sun.max.fill", color: C.sun,
      value: power(d.pv_kw), zero: isZero(d.pv_kw),
      sub: isZero(d.balcony_kw) ? null : `+ Balkon ${power(d.balcony_kw)}`,
    },
    {
      label: "Verbrauch", short: "Haus", sym: "house.fill", color: C.cons,
      value: power(d.consumption_kw), zero: isZero(d.consumption_kw),
    },
    {
      label: "Netzbezug", short: "Bezug", mid: "Bezug", sym: "arrow.down.circle", color: C.grid,
      value: power(d.grid_import_kw), zero: isZero(d.grid_import_kw),
    },
    {
      label: "Einspeisung", short: "Einsp.", mid: "Einsp.", sym: "arrow.up.circle", color: C.export,
      value: power(d.grid_export_kw), zero: isZero(d.grid_export_kw),
    },
    {
      label: "Batterie", short: "Akku", sym: batterySymbol(d.battery_soc_pct, charging),
      fallback: "battery.25", color: C.soc,
      value: pct(d.battery_soc_pct), zero: false,
      sub: resting ? "ruht" : `${charging ? "↑" : "↓"} ${power(d.battery_kw)}`,
    },
    {
      label: "Wärmepumpe", short: "WP", mid: "WP", sym: "heat.waves", fallback: "thermometer",
      color: C.hp, value: power(d.heatpump_kw), zero: isZero(d.heatpump_kw),
    },
    {
      label: "Wallbox", short: "Wallbox", sym: "bolt.car.fill", fallback: "car.fill",
      color: C.wb, value: power(d.wallbox_kw), zero: isZero(d.wallbox_kw),
      sub: !isZero(d.wallbox_kw) ? "lädt" : d.wallbox_vehicle ? "Auto da" : "kein Auto",
    },
  ];
}

// The balcony unit: solar like the roof, so the same orange, with a
// panel rather than the sun so the two tiles are told apart. Its note is what it made today, which says more about a 600 W
// plant than the moment's wattage does.
function balcony(d) {
  const today = d.today && d.today.balcony_kwh;
  return {
    label: "Balkon", short: "Balkon", sym: "solar.panel.fill", fallback: "sun.horizon.fill", color: C.sun,
    value: power(d.balcony_kw), zero: isZero(d.balcony_kw),
    sub: today === null || today === undefined ? null : `${today.toFixed(2)} kWh`,
  };
}

// The line that says how old the numbers are, and says it loudly when
// they are old: a widget is read at a glance, and a stale 4 kW looks
// exactly like a live one.
function freshness(state) {
  const ts = parseTs(state.data.timestamp);
  const ageMin = (Date.now() - ts.getTime()) / 60000;
  if (state.offline) return { text: `offline · ${clock(ts)}`, warn: true };
  if (ageMin > STALE_MIN) return { text: `alt · ${clock(ts)}`, warn: true };
  return { text: clock(ts), warn: false };
}

// ------------------------------------------------------------ building blocks
function addSymbol(stack, name, fallback, color, size) {
  const sym = SFSymbol.named(name) || (fallback ? SFSymbol.named(fallback) : null);
  if (!sym) return null;
  sym.applyFont(Font.semiboldSystemFont(size));
  const img = stack.addImage(sym.image);
  img.imageSize = new Size(size + 3, size + 3);
  img.applyFittingContentMode();
  img.tintColor = color;
  return img;
}

function addText(stack, str, font, color, scale = 0.6) {
  const t = stack.addText(str);
  t.font = font;
  t.textColor = color;
  t.lineLimit = 1;
  t.minimumScaleFactor = scale;
  return t;
}

// Widget sizes are not exposed, so they come from Apple's published sizes
// per screen width (and a measured one for the 402 pt screens). An unknown
// width takes the next smaller row: a little slack looks fine, a clipped
// value does not.
const WIDGET_SIZES = [
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
  const [, small, wide, tall] = WIDGET_SIZES.find(([w]) => sw >= w);
  if (family === "small") return new Size(small, small);
  if (family === "medium") return new Size(wide, small);
  return new Size(wide, tall);
}

function header(widget, state, title, fontSize) {
  const row = widget.addStack();
  row.centerAlignContent();
  addSymbol(row, "bolt.fill", null, C.pv, fontSize - 1);
  row.addSpacer(3);
  addText(row, title, F.semi(fontSize), C.ink);
  row.addSpacer();
  const f = freshness(state);
  addText(row, f.text, F.label(fontSize - 1), f.warn ? C.warn : C.ink2);
}

function emptyWidget(widget, family) {
  const small = family === "small";
  addText(widget, "Keine Verbindung", F.semi(small ? 13 : 15), C.ink);
  widget.addSpacer(4);
  const hint = widget.addText("Pi nicht erreichbar.\nTailscale an?");
  hint.font = F.label(small ? 11 : 12);
  hint.textColor = C.ink2;
  widget.addSpacer();
}

// ------------------------------------------------------------ small: a list
// Seven rows under a header in about 140 pt. 14 pt, and smaller only on a
// phone whose widget is too short for seven rows of that.
function buildSmall(widget, state) {
  const pad = 11;
  widget.setPadding(pad, pad + 1, pad, pad + 1);
  const size = widgetSize("small");
  header(widget, state, "Energie", 11.5);
  widget.addSpacer(4);
  const rowH = (size.height - 2 * pad - 14 - 4) / 7;
  const valueSize = Math.min(SIZE.smallValue, Math.floor((rowH / 1.2) * 2) / 2);
  const rows = metrics(state.data);
  rows.forEach((m) => {
    const row = widget.addStack();
    row.centerAlignContent();
    const icon = row.addStack();
    icon.size = new Size(valueSize + 5, 0);
    addSymbol(icon, m.sym, m.fallback, m.color, valueSize - 3);
    addText(row, m.short, F.label(valueSize - 1.5), C.ink2);
    row.addSpacer();
    addText(row, m.value, F.semi(valueSize), m.zero ? C.dim : C.ink);
  });
  widget.addSpacer();
}

// ------------------------------------------------------------ tiles
// 4x2: heading and icon on the right of the top row, the value below on
// the left. A tile is about 70 pt wide, so the long words get their short
// form.
function tileMedium(parent, m, width, height) {
  const t = parent.addStack();
  t.layoutVertically();
  t.size = new Size(width, height);
  t.backgroundColor = C.tile;
  t.cornerRadius = 10;
  t.setPadding(6, 7, 6, 6);

  const top = t.addStack();
  top.centerAlignContent();
  top.addSpacer();
  addText(top, m.mid || m.label, F.label(10.5), C.ink2, 0.75);
  top.addSpacer(3);
  addSymbol(top, m.sym, m.fallback, m.color, 12);

  t.addSpacer();
  addText(t, m.value, F.value(SIZE.value), m.zero ? C.dim : C.ink, 0.65);
  addText(t, m.sub || " ", F.label(10), m.subWarn ? C.warn : C.ink2, 0.75);
}

// 2x4: the value on the left with its note under it; icon and heading
// stacked on the right.
function tileLarge(parent, m, width, height) {
  const t = parent.addStack();
  t.size = new Size(width, height);
  t.backgroundColor = C.tile;
  t.cornerRadius = 11;
  t.setPadding(4, 9, 4, 8);
  t.centerAlignContent();

  const left = t.addStack();
  left.layoutVertically();
  addText(left, m.value, F.value(SIZE.value), m.zero ? C.dim : C.ink, 0.7);
  addText(left, m.sub || " ", F.label(SIZE.sub), m.subWarn ? C.warn : C.ink2, 0.9);

  t.addSpacer();
  const right = t.addStack();
  right.layoutVertically();
  // As wide as its heading and no wider. Without a width, the flexible
  // spacers that right-align icon and heading make this column claim all
  // the room it can, and iOS then shrinks the value and the note on the
  // left to make up for it. The width is estimated from the heading's
  // length at 12 pt; a heading a little wider shrinks a little itself.
  // Never narrower than the icon: "PV" alone is narrower than the sun.
  right.size = new Size(Math.max(30, Math.ceil(m.label.length * 7) + 4), 0);
  const iconRow = right.addStack();
  iconRow.addSpacer();
  addSymbol(iconRow, m.sym, m.fallback, m.color, 16);
  right.addSpacer(3);
  const labelRow = right.addStack();
  labelRow.addSpacer();
  addText(labelRow, m.label, F.label(SIZE.label), C.ink2, 0.75);
}

// ------------------------------------------------------------ medium: 4 x 2 tiles
function buildMedium(widget, state) {
  const pad = 11;
  const gap = 5;
  widget.setPadding(pad, pad, pad, pad);
  const size = widgetSize("medium");
  const cellW = (size.width - 2 * pad - 3 * gap) / 4;
  const cellH = (size.height - 2 * pad - gap) / 2;

  const d = state.data;
  const f = freshness(state);
  const cells = metrics(d);
  // The eighth tile carries the day's autarky and the time of the reading,
  // which on this size has no header to go in.
  cells.push({
    label: "Autarkie", sym: "leaf.fill", color: C.export,
    value: pct(d.today && d.today.autarky_pct), zero: false,
    sub: `heute · ${f.text}`, subWarn: f.warn,
  });

  for (let r = 0; r < 2; r++) {
    const row = widget.addStack();
    row.spacing = gap;
    for (let c = 0; c < 4; c++) tileMedium(row, cells[r * 4 + c], cellW, cellH);
    if (r === 0) widget.addSpacer(gap);
  }
}

// ------------------------------------------------------------ large: 2 x 4 tiles and today
function buildLarge(widget, state) {
  const pad = 13;
  const gap = 5;
  widget.setPadding(pad, pad, pad, pad);
  const size = widgetSize("large");
  const innerW = size.width - 2 * pad;
  const d = state.data;

  header(widget, state, "Energie jetzt", 14);
  widget.addSpacer(7);

  const todayH = 52;
  const headerH = 17 + 7;
  const sectionH = 16 + 4;
  const above = 8;
  const tilesH = size.height - 2 * pad - headerH - sectionH - todayH - above;
  const cellW = (innerW - gap) / 2;
  const cellH = (tilesH - 3 * gap) / 4;
  const cells = metrics(d);
  // The balcony has a tile of its own here, so the PV tile does not
  // repeat it.
  cells[0].sub = null;
  cells.push(balcony(d));
  for (let r = 0; r < 4; r++) {
    const row = widget.addStack();
    row.spacing = gap;
    for (let c = 0; c < 2; c++) tileLarge(row, cells[r * 2 + c], cellW, cellH);
    if (r < 3) widget.addSpacer(gap);
  }

  widget.addSpacer(above);
  const title = widget.addStack();
  title.centerAlignContent();
  addText(title, "Heute", F.semi(13), C.ink);
  title.addSpacer(4);
  addText(title, "kWh", F.label(12), C.ink2);
  title.addSpacer();
  const sc = d.today && d.today.self_consumption_pct;
  if (sc !== null && sc !== undefined) {
    addText(title, `Eigenverbrauch ${pct(sc)}`, F.label(12), C.ink2);
  }
  widget.addSpacer(4);

  const t = d.today || {};
  const today = [
    { label: "PV", value: num(t.pv_kwh), color: C.sun, sym: "sun.max.fill" },
    { label: "Verbrauch", value: num(t.consumption_kwh), color: C.cons, sym: "house.fill" },
    { label: "Bezug", value: num(t.import_kwh), color: C.grid, sym: "arrow.down.circle" },
    { label: "Einsp.", value: num(t.export_kwh), color: C.export, sym: "arrow.up.circle" },
    { label: "Autarkie", value: pct(t.autarky_pct), color: C.export, sym: "leaf.fill" },
  ];
  const strip = widget.addStack();
  strip.size = new Size(innerW, todayH);
  strip.backgroundColor = C.tile;
  strip.cornerRadius = 10;
  strip.setPadding(6, 8, 6, 8);
  strip.centerAlignContent();
  today.forEach((m, i) => {
    const col = strip.addStack();
    col.layoutVertically();
    const top = col.addStack();
    top.centerAlignContent();
    addSymbol(top, m.sym, null, m.color, 11);
    top.addSpacer(2);
    addText(top, m.label, F.label(SIZE.label - 1), C.ink2, 0.7);
    col.addSpacer(2);
    addText(col, m.value, F.value(SIZE.value), C.ink, 0.7);
    if (i < today.length - 1) strip.addSpacer();
  });
}

// ------------------------------------------------------------ lock screen
function buildAccessory(widget, state, family) {
  const d = state.data;
  const charging = d.battery_kw > 0.01;
  if (family === "accessoryInline") {
    widget.addText(
      `☀ ${power(d.pv_kw)} · ⌂ ${power(d.consumption_kw)} · ${pct(d.battery_soc_pct)}`
    );
    return;
  }
  if (family === "accessoryCircular") {
    widget.addAccessoryWidgetBackground = true;
    const s = widget.addStack();
    s.layoutVertically();
    const top = s.addStack();
    top.addSpacer();
    addSymbol(top, batterySymbol(d.battery_soc_pct, charging), "battery.25", Color.white(), 11);
    top.addSpacer();
    const mid = s.addStack();
    mid.addSpacer();
    addText(mid, `${Math.round(d.battery_soc_pct)}%`, F.value(15), Color.white());
    mid.addSpacer();
    return;
  }
  // accessoryRectangular: three lines, two values each.
  const pairs = [
    [["sun.max.fill", power(d.pv_kw)], ["house.fill", power(d.consumption_kw)]],
    [["arrow.down.circle", power(d.grid_import_kw)], ["arrow.up.circle", power(d.grid_export_kw)]],
    [[batterySymbol(d.battery_soc_pct, charging), pct(d.battery_soc_pct)], ["heat.waves", power(d.heatpump_kw)]],
  ];
  pairs.forEach((pair) => {
    const row = widget.addStack();
    row.centerAlignContent();
    pair.forEach(([sym, value]) => {
      const cell = row.addStack();
      cell.size = new Size(70, 0);
      cell.centerAlignContent();
      addSymbol(cell, sym, "circle.fill", Color.white(), 10);
      cell.addSpacer(3);
      addText(cell, value, F.semi(13), Color.white(), 0.7);
      cell.addSpacer();
    });
  });
}

// ------------------------------------------------------------ assemble
function build(family, state) {
  const widget = new ListWidget();
  const accessory = String(family).startsWith("accessory");
  if (!accessory) widget.backgroundColor = C.bg;
  widget.refreshAfterDate = new Date(Date.now() + REFRESH_MIN * 60 * 1000);
  if (!state) {
    if (accessory) widget.addText("Energie offline");
    else emptyWidget(widget, family);
    return widget;
  }
  widget.url = `${state.host}/`;
  if (accessory) buildAccessory(widget, state, family);
  else if (family === "small") buildSmall(widget, state);
  else if (family === "medium") buildMedium(widget, state);
  else buildLarge(widget, state);
  return widget;
}

// ------------------------------------------------------------ self-update
// The script keeps itself current: at most once an hour (every time when
// opened in the app) it fetches the version the Pi serves and, if that
// differs, writes it over its own file. The new version takes effect on
// the next run; this run finishes with the code it started with.
//
// A broken download must not be able to replace a working script, because
// a script that does not run cannot update itself back. So the new code
// has to start like this file, carry filled-in addresses, and parse,
// before it is written.
const UPDATE_EVERY_H = 1;
const MARKER = "// Energie - the Home Energy Manager as an iPhone widget";
const UPDATE_STAMP = "energie-widget-update.txt";

async function selfUpdate(host, force) {
  try {
    const own = module.filename;
    const scripts = own.includes("Mobile Documents") ? FileManager.iCloud() : FileManager.local();
    const stamp = fm.joinPath(fm.documentsDirectory(), UPDATE_STAMP);
    if (!force && fm.fileExists(stamp)) {
      const last = Number(fm.readString(stamp)) || 0;
      if (Date.now() - last < UPDATE_EVERY_H * 3600 * 1000) return "skipped";
    }
    const req = new Request(`${host}/widget/scriptable.js`);
    req.timeoutInterval = TIMEOUT_S;
    const code = await req.loadString();
    if (!req.response || req.response.statusCode !== 200) return "failed";
    fm.writeString(stamp, String(Date.now()));

    if (!code.startsWith(MARKER) || !/const HOSTS = \["http/.test(code)) return "failed";
    const norm = (s) => s.replace(/\r\n/g, "\n");
    if (norm(code) === norm(scripts.readString(own))) return "current";
    // Parse only - constructing the function does not run it.
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
const inApp = !(config.runsInWidget || config.runsInAccessoryWidget);
const update = state && !state.offline ? await selfUpdate(state.host, inApp) : "offline";

if (!inApp) {
  Script.setWidget(build(config.widgetFamily || "medium", state));
} else {
  // Run inside the app: a preview of each size, and an update check.
  const pick = new Alert();
  pick.title = "Vorschau";
  pick.message = UPDATE_TEXT[update] || "";
  pick.addAction("Klein (2x2)");
  pick.addAction("Mittel (4x2)");
  pick.addAction("Groß (4x4)");
  pick.addCancelAction("Fertig");
  const choice = await pick.presentSheet();
  if (choice === 0) await build("small", state).presentSmall();
  if (choice === 1) await build("medium", state).presentMedium();
  if (choice === 2) await build("large", state).presentLarge();
}
Script.complete();

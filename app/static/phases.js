// Phase current at the house connection, against the main fuse.
//
// Why this exists: the clamp meter at the connection point measures the
// one quantity the inverter cannot report. Its total duplicates the
// inverter's grid reading to within 16 W, which is a cross-check that has
// now been passed seven thousand times and has little left to prove - but
// current per phase it alone can see, and it had already recorded 22 A on
// one phase of a 25 A fuse without anything on these pages saying so.
//
// Why the peak and not the live value: a fuse is not tripped by an
// average, and nobody is watching a dashboard at half past nine at night
// when the car and the heat pump happen to start together. A live gauge
// would have shown that 22 A to an empty room. So each phase draws its
// peak as a marker, the window's peak and the worst ever are both printed,
// and the sentence at the top leads with the worst ever rather than with
// whatever is happening this minute.
//
// One colour and no status palette. The threshold is drawn as a line on
// the track, so a bar that has gone too far reads as a position rather
// than as a hue - the same device as the heating window on the compressor
// chart, and it survives both color-blindness and a monochrome print. The
// verdict line carries a dot, but it carries its own sentence too.
//
// The helpers (el, fmt, cssVar, chartWidth, onViewportResize, setHidden)
// come from app.js, which this page loads first.

const PH_ROW_H = 34;
const PH_BAR_H = 16;

let phases = null;

function renderPhases() {
  if (!phases) return;
  const card = el("phaseCard");
  if (!card) return;
  // Without a fuse rating there is no distance to show, and without a
  // clamp there is nothing to show it for.
  if (!phases.fuse_a || !phases.phases.some((p) => p.ever_a !== null)) {
    setHidden(card, true);
    return;
  }
  setHidden(card, false);
  phRenderVerdict();
  phRenderBars();
  phRenderTable();
}

function phPct(amps) {
  return (100 * (amps || 0)) / phases.fuse_a;
}

function phRenderVerdict() {
  const node = el("phaseVerdict");
  if (!node) return;
  const worst = phases.worst_ever;
  const warn = phases.warn_pct;
  let color = cssVar("--success");
  let text;
  if (!worst) {
    text = "No phase current recorded yet.";
  } else {
    const pct = phPct(worst.amps);
    const when = worst.at
      ? new Date(worst.at).toLocaleString("en-GB", {
          day: "numeric",
          month: "long",
          hour: "2-digit",
          minute: "2-digit",
        })
      : null;
    if (pct >= 100) color = cssVar("--status-error");
    else if (pct >= warn) color = cssVar("--status-warning");
    text =
      `Highest ever on a single phase: ${fmt(worst.amps, 1)} A on ${worst.name}, ` +
      `${fmt(pct, 0)}% of the ${fmt(phases.fuse_a, 0)} A fuse` +
      (when ? `, on ${when}` : "") +
      ". " +
      (pct >= warn
        ? "That is close enough that another large load wants thinking about."
        : "There is room.");
  }
  node.innerHTML =
    `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color};margin-right:7px;"></span>` +
    phEscape(text);
}

function phEscape(text) {
  return String(text).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );
}

function phRenderBars() {
  const wrap = el("phaseWrap");
  if (!wrap) return;

  const width = chartWidth(wrap);
  const padL = 26;
  const padR = 58;
  const plot = Math.max(40, width - padL - padR);
  // The axis is the fuse, not the largest value: the whole point is the
  // distance to a fixed limit, so the scale may not rescale itself. A
  // reading above the rating would be a tripped fuse, so it is clamped and
  // the number beside it still says what was measured.
  const x = (amps) => padL + Math.min(1, (amps || 0) / phases.fuse_a) * plot;
  const height = phases.phases.length * PH_ROW_H + 18;
  const muted = cssVar("--text-muted");
  const warnX = padL + (phases.warn_pct / 100) * plot;

  let svg = "";
  phases.phases.forEach((phase, i) => {
    const rowY = i * PH_ROW_H;
    const barY = rowY + (PH_ROW_H - PH_BAR_H) / 2;
    // The track is the fuse's whole rating.
    svg += `<rect x="${padL}" y="${barY}" width="${plot}" height="${PH_BAR_H}" rx="3" fill="${cssVar(
      "--gridline"
    )}" />`;
    if (phase.now_a !== null) {
      svg += `<rect x="${padL}" y="${barY}" width="${Math.max(
        2,
        x(phase.now_a) - padL
      )}" height="${PH_BAR_H}" rx="3" fill="${cssVar("--series-grid")}" />`;
    }
    // The peak inside the window, as a marker: where this phase has
    // actually been, which the live bar forgets a minute later.
    if (phase.peak_a !== null && phase.peak_a > (phase.now_a || 0)) {
      svg += `<rect x="${x(phase.peak_a) - 1}" y="${barY - 3}" width="2" height="${
        PH_BAR_H + 6
      }" fill="${cssVar("--text-secondary")}" />`;
    }
    svg += `<text x="0" y="${barY + PH_BAR_H - 3}" font-size="12" font-weight="600" fill="${cssVar(
      "--text-secondary"
    )}">${phase.name}</text>`;
    svg += `<text x="${width - padR + 6}" y="${barY + PH_BAR_H - 3}" font-size="11" fill="${cssVar(
      "--text-primary"
    )}">${phase.now_a === null ? "-" : `${fmt(phase.now_a, 1)} A`}</text>`;
  });

  // The threshold, over the bars so it is never buried by one, and the
  // rating at the end of the axis.
  svg += `<line x1="${warnX}" y1="0" x2="${warnX}" y2="${
    phases.phases.length * PH_ROW_H
  }" stroke="${cssVar("--baseline")}" stroke-width="1" stroke-dasharray="3 3" />`;
  svg += `<text x="${warnX}" y="${phases.phases.length * PH_ROW_H + 12}" font-size="10" ` +
    `text-anchor="middle" fill="${muted}">${fmt(phases.warn_pct, 0)}%</text>`;
  svg += `<text x="${padL + plot}" y="${phases.phases.length * PH_ROW_H + 12}" font-size="10" ` +
    `text-anchor="end" fill="${muted}">${fmt(phases.fuse_a, 0)} A fuse</text>`;

  wrap.innerHTML = `
    <svg class="chart" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img"
         aria-label="Current per phase against the main fuse rating">
      ${svg}
    </svg>`;

  const legend = el("phaseLegend");
  if (legend) {
    renderLegend(legend, [
      { name: "Drawing now", color: "--series-grid" },
      { name: `Peak in the last ${fmt(phases.hours, 0)} h (marker)`, color: "--text-secondary" },
      { name: `${fmt(phases.warn_pct, 0)}% of the fuse (dashed)`, color: "--baseline" },
    ]);
  }
}

function phRenderTable() {
  const host = el("phaseTable");
  if (!host) return;
  host.innerHTML = `
    <table class="data">
      <thead><tr>
        <th>Phase</th><th>Now</th><th>Peak ${fmt(phases.hours, 0)} h</th>
        <th>Highest ever</th><th>Headroom now</th>
      </tr></thead>
      <tbody>${phases.phases
        .map(
          (p) => `<tr>
            <td>${p.name}</td>
            <td>${p.now_a === null ? "-" : `${fmt(p.now_a, 1)} A`}</td>
            <td>${p.peak_a === null ? "-" : `${fmt(p.peak_a, 1)} A`}</td>
            <td>${
              p.ever_a === null ? "-" : `${fmt(p.ever_a, 1)} A (${fmt(phPct(p.ever_a), 0)}%)`
            }</td>
            <td>${
              p.now_a === null ? "-" : `${fmt(phases.fuse_a - p.now_a, 1)} A`
            }</td>
          </tr>`
        )
        .join("")}</tbody>
    </table>
    <p class="chart-note">
      Measured at the house connection by the clamp meter, which is the only
      instrument here that sees phase current - the inverter reports load per
      phase but not what the connection carries. The fuse rating is a
      setting, not a reading: nothing here protects anything, it only shows
      the distance to the thing that does.
    </p>`;
}

async function fetchPhases(hours = 24) {
  try {
    const res = await fetch(`/api/phases?hours=${hours}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    phases = await res.json();
    renderPhases();
  } catch (err) {
    const wrap = el("phaseWrap");
    if (wrap) {
      wrap.innerHTML = `<div class="empty-state">Failed to load phase currents (${err.message})</div>`;
    }
  }
}

onViewportResize(() => renderPhases());
fetchPhases();
// The live bar wants the dashboard's own rhythm; the peaks move rarely.
setInterval(() => fetchPhases(), 60000);

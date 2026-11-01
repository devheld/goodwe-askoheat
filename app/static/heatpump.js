// Compressor runs of the heat pump, one row per day against the window the
// pump is meant to heat in.
//
// Why this form: the question is not how much the pump used - the Energy
// page answers that - it is WHEN it ran, and whether that was inside the
// hours the time program was set to. Both are positions on a clock, so the
// chart is a clock: 00:00 on the left, 24:00 on the right, the intended
// window shaded, one row per day. A run outside the window then needs no
// color of its own and no label to be read as wrong; it is simply a bar
// that sits outside the band. The rows underneath each other turn a change
// at the controller into a before/after that can be seen rather than
// remembered.
//
// The helpers (el, fmt, cssVar, chartWidth, onViewportResize, renderLegend)
// come from app.js, which this page loads first.

const HP_ROW_H = 24;
const HP_BAR_H = 13;

let hpRuns = null;

function hpDayKey(date) {
  // Local calendar day, built from the local getters rather than from an
  // ISO slice: the timestamps are UTC, and 00:30 local is still the
  // previous UTC day in winter.
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(
    date.getDate()
  ).padStart(2, "0")}`;
}

function hpStartOfDay(date) {
  const d = new Date(date);
  d.setHours(0, 0, 0, 0);
  return d;
}

// The night we are in, or the one that just ended: from the hour the window
// closes to the hour it opens again. This is the span the question "did
// anything reheat while we slept" is actually about, and it straddles
// midnight, so it is not a calendar day.
function hpLastNight(now, fromH, toH) {
  const start = new Date(now);
  start.setHours(toH, 0, 0, 0);
  if (now.getHours() < toH) start.setDate(start.getDate() - 1);
  const end = new Date(start);
  end.setDate(end.getDate() + 1);
  end.setHours(fromH, 0, 0, 0);
  return { start, end, open: now < end };
}

// How much of a run fell inside a span. A compressor at its lower limit
// holds its power flat for the whole run - measured at 1.10 to 1.21 kW
// across every run so far - so sharing the energy out by time is accurate
// here rather than merely convenient.
function hpOverlapKwh(run, from, to) {
  const start = new Date(run.start).getTime();
  const end = new Date(run.end).getTime();
  const total = Math.max(end - start, 1);
  const overlap = Math.min(end, to.getTime()) - Math.max(start, from.getTime());
  if (overlap <= 0) return 0;
  return (run.kwh * overlap) / total;
}

function hpEscape(text) {
  return String(text).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );
}

function hpTime(date) {
  return date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

// Rows for every day in the range, including the ones with nothing in them.
// An empty row is the result the time program is supposed to produce, so it
// has to be visible - a chart that only showed days with runs would hide
// exactly the evidence that a setting worked.
function hpDayRows(payload) {
  const rows = new Map();
  const first = hpStartOfDay(new Date(payload.since));
  const today = hpStartOfDay(new Date());
  for (let d = new Date(first); d <= today; d.setDate(d.getDate() + 1)) {
    rows.set(hpDayKey(d), { day: new Date(d), segments: [], kwh: 0, runs: 0 });
  }

  for (const run of payload.runs) {
    const start = new Date(run.start);
    const end = new Date(run.end);
    // A run that crosses midnight belongs to both rows, drawn as the part
    // that happened on each. Assigning it whole to its starting day would
    // put a bar at 23:50 and leave the hour after midnight blank.
    let segStart = start;
    while (segStart < end) {
      const dayStart = hpStartOfDay(segStart);
      const nextDay = new Date(dayStart);
      nextDay.setDate(nextDay.getDate() + 1);
      const segEnd = end < nextDay ? end : nextDay;
      const row = rows.get(hpDayKey(dayStart));
      if (row) {
        row.segments.push({ from: segStart, to: segEnd, run });
        row.kwh += hpOverlapKwh(run, segStart, segEnd);
        row.runs += segStart.getTime() === start.getTime() ? 1 : 0;
      }
      segStart = segEnd;
    }
  }

  return [...rows.values()].reverse();
}

function renderHeatpumpRuns() {
  const card = el("heatpumpRunsCard");
  const wrap = el("heatpumpRunsWrap");
  if (!card || !wrap || !hpRuns) return;
  // No clamp meter on the pump's feed yet, or not for this range: the card
  // stays away rather than claiming the pump never ran.
  if (!hpRuns.totals.measured_hours) {
    card.hidden = true;
    return;
  }
  card.hidden = false;

  const { from_h: fromH, to_h: toH } = hpRuns.window;
  const rows = hpDayRows(hpRuns);
  const width = chartWidth(wrap);
  const narrow = width < 560;
  const padL = narrow ? 50 : 76;
  const padR = narrow ? 46 : 60;
  const padT = 6;
  const padB = 24;
  const innerW = width - padL - padR;
  const height = rows.length * HP_ROW_H + padT + padB;
  const x = (hours) => padL + (hours / 24) * innerW;

  const grid = cssVar("--gridline");
  const muted = cssVar("--text-muted");
  const secondary = cssVar("--text-secondary");
  const runColor = cssVar("--series-heatpump");

  // The window, drawn once behind everything as one block: it is the same
  // hours on every row, and a band per row would read as a mark rather
  // than as the backdrop it is.
  let svg = `<rect x="${x(fromH)}" y="${padT}" width="${x(toH) - x(fromH)}" height="${
    rows.length * HP_ROW_H
  }" fill="${grid}" />`;

  const ticks = narrow ? [0, 6, 12, 18, 24] : [0, 3, 6, 9, 12, 15, 18, 21, 24];
  for (const h of ticks) {
    svg += `<line x1="${x(h)}" x2="${x(h)}" y1="${padT}" y2="${
      padT + rows.length * HP_ROW_H
    }" stroke="${grid}" stroke-width="1" />`;
  }

  const hits = [];
  rows.forEach((row, i) => {
    const rowY = padT + i * HP_ROW_H;
    const barY = rowY + (HP_ROW_H - HP_BAR_H) / 2;
    const label = row.day.toLocaleDateString("en-GB", {
      weekday: narrow ? undefined : "short",
      day: "2-digit",
      month: "2-digit",
    });
    svg += `<text x="${padL - 8}" y="${rowY + HP_ROW_H / 2 + 4}" text-anchor="end" font-size="11" fill="${secondary}">${label}</text>`;

    for (const seg of row.segments) {
      const h0 = (seg.from - hpStartOfDay(seg.from)) / 3600000;
      const h1 = h0 + (seg.to - seg.from) / 3600000;
      const x0 = x(h0);
      // A 30-minute run is 1.4% of the row. Floored at 2px so a short run
      // is a visible mark instead of rounding away to nothing.
      const w = Math.max(x(h1) - x0, 2);
      svg += `<rect x="${x0}" y="${barY}" width="${w}" height="${HP_BAR_H}" rx="3" fill="${runColor}" />`;
      const run = seg.run;
      const tip =
        `${new Date(run.start).toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "2-digit" })} ` +
        `${hpTime(new Date(run.start))}-${hpTime(new Date(run.end))} · ` +
        `${fmt(run.minutes, 0)} min · ${fmt(run.kwh, 2)} kWh · ${fmt(run.mean_kw, 2)} kW mean` +
        (run.inside ? "" : ` · ${fmt(run.kwh_outside, 2)} kWh outside the window`);
      // A 13px bar is a poor hit target, so the whole row height around it
      // takes the hover.
      hits.push(
        `<rect x="${x0}" y="${rowY}" width="${w}" height="${HP_ROW_H}" fill="transparent" class="hp-hit" data-tip="${hpEscape(tip)}" />`
      );
    }

    if (row.kwh > 0.005) {
      svg += `<text x="${width - padR + 8}" y="${rowY + HP_ROW_H / 2 + 4}" font-size="11" fill="${secondary}">${fmt(row.kwh, 1)} kWh</text>`;
    } else {
      svg += `<text x="${width - padR + 8}" y="${rowY + HP_ROW_H / 2 + 4}" font-size="11" fill="${muted}">-</text>`;
    }
  });

  for (const h of ticks) {
    svg += `<text x="${x(h)}" y="${height - 7}" text-anchor="middle" font-size="11" fill="${muted}">${String(h).padStart(2, "0")}</text>`;
  }

  wrap.innerHTML = `
    <svg class="chart" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      ${svg}
      ${hits.join("")}
    </svg>`;

  hpAttachTooltip(wrap);
  renderLegend(el("heatpumpRunsLegend"), [
    { name: "Compressor running", color: "--series-heatpump" },
    {
      name: `Window it is set to heat in (${String(fromH).padStart(2, "0")}:00-${String(toH).padStart(2, "0")}:00)`,
      color: "--gridline",
    },
  ]);
  hpRenderNote();
  hpRenderTable();
}

function hpAttachTooltip(wrap) {
  const svg = wrap.querySelector("svg");
  const tooltip = document.createElement("div");
  tooltip.style.cssText = `
    position: absolute; pointer-events: none; display: none;
    background: ${cssVar("--surface-1")}; border: 1px solid ${cssVar("--border")};
    border-radius: 8px; padding: 6px 10px; font-size: 12px; color: ${cssVar("--text-primary")};
    box-shadow: 0 2px 8px rgba(0,0,0,0.15); max-width: 280px; z-index: 10;
  `;
  wrap.style.position = "relative";
  wrap.appendChild(tooltip);

  svg.addEventListener("mousemove", (evt) => {
    const hit = evt.target.closest(".hp-hit");
    if (!hit) {
      tooltip.style.display = "none";
      return;
    }
    tooltip.textContent = hit.dataset.tip;
    tooltip.style.display = "block";
    const rect = wrap.getBoundingClientRect();
    let left = evt.clientX - rect.left + 12;
    if (left + 280 > rect.width) left = Math.max(0, rect.width - 290);
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${Math.max(0, evt.clientY - rect.top - 44)}px`;
  });
  svg.addEventListener("mouseleave", () => {
    tooltip.style.display = "none";
  });
}

function hpRenderNote() {
  const note = el("heatpumpRunsNote");
  const verdict = el("heatpumpRunsVerdict");
  const { from_h: fromH, to_h: toH } = hpRuns.window;
  if (note) {
    note.textContent =
      `Measured on the pump's own feed. Its controller cannot be read at all - the ` +
      `gateway speaks no Modbus - so the clamp meter is the only witness to what it ` +
      `decided. A bar is the compressor drawing more than ${fmt(hpRuns.threshold_kw, 1)} kW; ` +
      `below that the machine is idling (its controller alone draws 35 W) or just ` +
      `circulating water.`;
  }
  if (!verdict) return;

  const night = hpLastNight(new Date(), fromH, toH);
  let kwh = 0;
  let count = 0;
  for (const run of hpRuns.runs) {
    const share = hpOverlapKwh(run, night.start, night.end);
    if (share > 0) {
      kwh += share;
      count += 1;
    }
  }
  const span = `${hpTime(night.start)} to ${hpTime(night.end)}`;
  const when = night.open ? "Tonight so far" : "Last night";
  // The one line to read at breakfast. The dot is backed by its own
  // sentence, so the state never rests on the color.
  const good = kwh < 0.05;
  const color = cssVar(good ? "--success" : "--status-warning");
  const text = good
    ? `${when}, ${span}: nothing ran.`
    : `${when}, ${span}: ${count} ${count === 1 ? "run" : "runs"}, ${fmt(kwh, 2)} kWh outside the heating window.`;
  verdict.innerHTML =
    `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color};margin-right:7px;"></span>` +
    hpEscape(text) +
    ` <span style="color:${cssVar("--text-muted")}">Controller standby over the whole range: ` +
    `${fmt(hpRuns.totals.standby_kwh, 2)} kWh.</span>`;
}

function hpRenderTable() {
  const host = el("heatpumpRunsTable");
  if (!host) return;
  if (!hpRuns.runs.length) {
    host.innerHTML = '<div class="empty-state">No compressor run recorded in this range.</div>';
    return;
  }
  host.innerHTML = `
    <table class="data">
      <thead><tr>
        <th>Start</th><th>End</th><th>Duration</th><th>Energy</th>
        <th>Mean</th><th>Peak</th><th>Window</th>
      </tr></thead>
      <tbody>${hpRuns.runs
        .map((run) => {
          const start = new Date(run.start);
          const end = new Date(run.end);
          return `<tr>
            <td>${start.toLocaleString("en-GB", { weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}</td>
            <td>${hpTime(end)}</td>
            <td>${fmt(run.minutes, 0)} min</td>
            <td>${fmt(run.kwh, 2)} kWh</td>
            <td>${fmt(run.mean_kw, 2)} kW</td>
            <td>${fmt(run.peak_kw, 2)} kW</td>
            <td>${run.inside ? "inside" : `outside (${fmt(run.kwh_outside, 2)} kWh)`}</td>
          </tr>`;
        })
        .join("")}</tbody>
    </table>`;
}

async function fetchHeatpumpRuns(days = 7) {
  try {
    const res = await fetch(`/api/heatpump/runs?days=${days}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    hpRuns = await res.json();
    renderHeatpumpRuns();
  } catch (err) {
    const wrap = el("heatpumpRunsWrap");
    if (wrap) {
      wrap.innerHTML = `<div class="empty-state">Failed to load compressor runs (${err.message})</div>`;
    }
  }
}

// Runs are hours long, so this does not need the dashboard's 30-second
// clock; and the chart is sized to its container at draw time, so a rotated
// phone redraws from the payload already in hand.
onViewportResize(() => renderHeatpumpRuns());
fetchHeatpumpRuns();
setInterval(() => fetchHeatpumpRuns(), 300000);

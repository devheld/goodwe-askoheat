// Finanzen: what the installation cost against what it has saved so far.
//
// Why this form: the question has exactly two quantities in the same unit -
// what went out and what has come back - so it is two bars on one scale,
// and the distance between their ends IS the answer. A progress ring or a
// percentage would hide that distance behind a number; two bars let it be
// read off directly, and the day the lower bar passes the upper one is the
// day the thing is paid off.
//
// One hue, not two. The investment is a neutral track because it is the
// target and not a measurement; the return is the one colored series.
// Both bars are labelled with their own name and amount, so identity never
// rests on color and no legend box is needed.
//
// Everything in the return is measured savings. The only projection is the
// break-even date, and it is printed next to the daily rate and the number
// of recorded days that rate comes from - a payback date drawn from eleven
// weeks of late-summer sun is an estimate wearing a calendar's clothes.
//
// The helpers (el, fmt, cssVar, chartWidth, onViewportResize) come from
// app.js, which this page loads first.

const FIN_BAR_H = 20;
const FIN_LABEL_H = 17;
const FIN_GROUP_GAP = 14;

let finance = null;

function finMoney(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  const cur = (finance && finance.currency) || "CHF";
  return `${value.toLocaleString("en-GB", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })} ${cur}`;
}

function finEscape(text) {
  return String(text).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );
}

function finDate(iso) {
  return new Date(iso).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "long",
    year: "numeric",
  });
}

function renderFinance() {
  if (!finance) return;
  const card = el("financeCard");
  if (!card) return;
  setHidden(card, false);
  finRenderVerdict();
  finRenderBars();
  finRenderForecast();
  finRenderTable();
}

function finRenderVerdict() {
  const node = el("financeVerdict");
  if (!node) return;
  const muted = cssVar("--text-muted");
  let color = cssVar("--baseline");
  let text;
  if (!finance.known) {
    // No invoice, no break-even. Saying so beats reporting a payback
    // against an investment of zero, which would read as instant profit.
    text = "The invoice is not entered yet, so there is nothing to pay off against.";
  } else if (finance.remaining_chf > 0) {
    text = `${finMoney(finance.remaining_chf, 0)} to go before the installation has paid for itself.`;
  } else {
    color = cssVar("--success");
    text = `Paid for itself. ${finMoney(-finance.remaining_chf, 0)} of profit so far.`;
  }
  node.innerHTML =
    `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color};margin-right:7px;"></span>` +
    finEscape(text) +
    (finance.progress_pct !== null && finance.known
      ? ` <span style="color:${muted}">${fmt(finance.progress_pct, 1)}% of the way.</span>`
      : "");
}

function finRenderBars() {
  const wrap = el("financeWrap");
  if (!wrap) return;

  const net = finance.investment.net;
  const back = finance.returned_chf;
  // Only bars that mean something: without an invoice there is no target
  // to draw, and the return bar then simply shows what has been saved.
  const bars = [];
  if (finance.known) {
    bars.push({
      name: "Net investment",
      value: net,
      fill: cssVar("--gridline"),
      stroke: cssVar("--baseline"),
    });
  }
  bars.push({
    name: "Saved so far",
    value: back,
    fill: cssVar("--series-grid-export"),
    stroke: null,
  });

  const width = chartWidth(wrap);
  const max = Math.max(...bars.map((b) => Math.abs(b.value)), 1);
  const group = FIN_LABEL_H + FIN_BAR_H + FIN_GROUP_GAP;
  const height = bars.length * group - FIN_GROUP_GAP;

  let svg = "";
  bars.forEach((bar, i) => {
    const top = i * group;
    const barY = top + FIN_LABEL_H;
    // A 2 px floor so a bar that is a rounding error from zero is still a
    // visible mark rather than nothing at all.
    const w = Math.max(2, (Math.abs(bar.value) / max) * width);
    svg += `<text x="0" y="${top + 12}" font-size="12" fill="${cssVar("--text-secondary")}">${finEscape(bar.name)}</text>`;
    svg += `<text x="${width}" y="${top + 12}" font-size="12" text-anchor="end" font-weight="600" fill="${cssVar("--text-primary")}">${finEscape(finMoney(bar.value, 0))}</text>`;
    svg += `<rect x="0" y="${barY}" width="${w}" height="${FIN_BAR_H}" rx="3" fill="${bar.fill}"${
      bar.stroke ? ` stroke="${bar.stroke}" stroke-width="1"` : ""
    } />`;
  });

  wrap.innerHTML = `
    <svg class="chart" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img"
         aria-label="Net investment against what has been saved so far">
      ${svg}
    </svg>`;
}

function finRenderForecast() {
  const node = el("financeForecast");
  if (!node) return;
  const rate = finance.rate_chf_per_day;
  const days = finance.measured.recorded_days;
  const parts = [];

  if (!finance.known) {
    parts.push(
      `Put the invoice total into <code>INVESTMENT_CHF</code> in the .env file and this becomes a break-even date. ` +
        `The grant (${finMoney(finance.investment.subsidy, 0)}) and the tax deduction ` +
        `(${finMoney(finance.investment.tax_deduction, 0)}) are already in.`
    );
  } else if (finance.days_to_break_even === null) {
    parts.push("Not earning at a measurable rate yet, so there is no date to give.");
  } else if (finance.days_to_break_even === 0) {
    parts.push(`Already past break-even, and earning ${finMoney(rate)} a day.`);
  } else {
    parts.push(
      `<strong>Break-even in ${fmt(finance.days_to_break_even, 0)} days &mdash; ` +
        `${finDate(finance.break_even_date)}.</strong>`
    );
  }

  // The rate and what it rests on, always: a forecast whose basis is not
  // stated is a promise rather than an estimate. Eleven weeks of late
  // summer are not a year, and both directions of that error are real -
  // no sun in January, no heating saved in July.
  if (finance.known && finance.days_to_break_even) {
    parts.push(
      `At ${finMoney(rate)} a day, measured over ${fmt(days, 0)} recorded days. ` +
        `A winter without sun and a summer without heating will both move that.`
    );
  }

  const streams = finance.streams
    .filter((s) => s.counted)
    .map((s) => `${finEscape(s.name)} ${fmt(s.chf, 2)}`)
    .join(" &middot; ");
  if (streams) parts.push(`Saved so far: ${streams} ${(finance.currency || "CHF")}.`);

  node.innerHTML = parts.join(" ");
}

function finRenderTable() {
  const host = el("financeTable");
  if (!host) return;
  const inv = finance.investment;
  const rows = [
    ["Invoice total", finMoney(inv.gross, 2), finance.known ? "" : "not entered yet"],
    ["Grant received", `-${finMoney(inv.subsidy, 2)}`, ""],
    [
      "Tax deduction",
      `-${finMoney(inv.tax_benefit, 2)}`,
      inv.tax_rate === 1
        ? `${finMoney(inv.tax_deduction, 0)} taken at face value - set TAX_DEDUCTION_RATE to the marginal tax rate for the money it actually saved`
        : `${fmt(100 * inv.tax_rate, 0)}% of ${finMoney(inv.tax_deduction, 0)}`,
    ],
    ["<strong>Net investment</strong>", `<strong>${finMoney(inv.net, 2)}</strong>`, ""],
  ];
  const streamRows = finance.streams.map((s) => [
    s.counted ? s.name : `${s.name} (not counted)`,
    finMoney(s.chf, 2),
    `${s.chf_per_day === null ? "-" : `${fmt(s.chf_per_day, 2)}/day`} &middot; ${finEscape(s.basis)}`,
  ]);

  host.innerHTML = `
    <table class="data">
      <thead><tr><th>Item</th><th>Amount</th><th>Note</th></tr></thead>
      <tbody>
        ${rows.map((r) => `<tr><td>${r[0]}</td><td>${r[1]}</td><td>${r[2]}</td></tr>`).join("")}
        ${streamRows
          .map((r) => `<tr><td>${finEscape(r[0])}</td><td>${r[1]}</td><td>${r[2]}</td></tr>`)
          .join("")}
      </tbody>
    </table>
    <p class="chart-note">
      Measured from ${finance.measured.first_record ? finDate(finance.measured.first_record) : "-"}
      ${
        finance.measured.earning_since
          ? `, although the installation has been earning since ${finDate(
              finance.measured.earning_since
            )} - the months before the recording started are not in the figures above.`
          : "."
      }
    </p>`;
}

async function fetchFinance() {
  try {
    const res = await fetch("/api/finance");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    finance = await res.json();
    renderFinance();
  } catch (err) {
    const wrap = el("financeWrap");
    if (wrap) {
      wrap.innerHTML = `<div class="empty-state">Failed to load the finance figures (${err.message})</div>`;
    }
  }
}

// A few francs a day: this does not belong on the dashboard's 30-second
// clock. Redrawn on resize from the payload already in hand.
onViewportResize(() => renderFinance());
fetchFinance();
setInterval(() => fetchFinance(), 600000);

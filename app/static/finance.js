// Finance: three paybacks, one row each.
//
// Why three. The roof and the charger are separate investments that
// happen to share a house. The roof earns its money from sunshine, the
// charger from kilometres driven, and a single blended rate belongs to
// neither of them: it reports the roof as about twice as good as it is
// and the charger as a fraction of what it is. So each sum is shown
// against the savings that actually came from it, and the combined figure
// is the third row rather than the only one.
//
// Why rows and not a chart. Each row has exactly two quantities in the
// same unit - what went out and what has come back - so the bar is a
// track the width of the investment and a fill the width of the return,
// and the gap between them IS the answer. Three of those stacked read in
// one pass; three charts would not. They are plain HTML rather than SVG
// because a bar that is one number wide needs no scale, no axis and no
// redraw on resize - the browser reflows it.
//
// One series colour and a neutral track. Every row carries its own name,
// its own figures and its own date, so identity never rests on colour and
// there is no legend to read. The only projection anywhere here is the
// break-even date, and each row prints the number of measured days its
// rate rests on - the car's fortnight is not the roof's eleven weeks.
//
// Everything else - the breakdown of the invoice, the tariff history, the
// benchmark against the quote, and each stream with what it was priced at
// - is in the table below the fold. The card itself stays short.
//
// The helpers (fmt, cssVar, money) come from chartkit.js, which this page
// loads first. This card is deliberately not period-scoped: the period
// buttons slice the charts above it, and a payback is a running total.

let finance = null;

function finEl(id) {
  return document.getElementById(id);
}

function finMoney(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  const cur = (finance && finance.currency) || "CHF";
  return `${value.toLocaleString("en-GB", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })} ${cur}`;
}

// Whole francs, no unit: the rows print a pair of these and the unit once.
function finInt(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return Math.round(value).toLocaleString("en-GB");
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

// A break-even date is accurate to a season at best, so the rows print a
// month and the day is left to the verdict line.
function finMonth(iso) {
  return new Date(iso).toLocaleDateString("en-GB", { month: "short", year: "numeric" });
}

function renderFinance() {
  if (!finance) return;
  const card = finEl("financeCard");
  if (!card) return;
  card.hidden = false;
  finRenderVerdict();
  finRenderRows();
  finRenderNote();
  finRenderBenchmark();
  finRenderTable();
}

function finRenderVerdict() {
  const node = finEl("financeVerdict");
  if (!node) return;
  const muted = cssVar("--text-muted");
  let color = cssVar("--text-muted");
  let text;
  if (!finance.known) {
    // No invoice, no break-even. Saying so beats reporting a payback
    // against an investment of zero, which would read as instant profit.
    text = "The invoice is not entered yet, so there is nothing to pay off against.";
  } else if (finance.remaining_chf > 0) {
    text = `${finMoney(finance.remaining_chf, 0)} to go on everything together`;
    if (finance.break_even_date) text += `, break-even ${finDate(finance.break_even_date)}`;
    text += ".";
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

function finRenderRows() {
  const wrap = finEl("financeWrap");
  if (!wrap) return;
  const groups = finance.groups || [];
  // Nothing to pay off, nothing to draw. A lone full-width bar would read
  // as a finished job, which is the exact wrong impression.
  if (!groups.length) {
    wrap.innerHTML = "";
    return;
  }
  const cur = finance.currency || "CHF";

  wrap.innerHTML =
    `<div class="payback">` +
    groups
      .map((g) => {
        const pct =
          g.progress_pct === null ? 0 : Math.max(0, Math.min(100, g.progress_pct));
        const says = [];
        if (g.rate_chf_per_day > 0) says.push(`${fmt(g.rate_chf_per_day, 2)}/day`);
        if (g.days_to_break_even === 0) says.push("paid off");
        else if (g.break_even_date) says.push(`break-even ${finMonth(g.break_even_date)}`);
        else says.push("no measurable rate yet");
        // The window the rate rests on, in the same breath as the date it
        // produced. A fortnight and eleven weeks are not the same promise,
        // and the combined row rests on both - so it names the range
        // rather than letting its shortest basis speak for the whole sum.
        const windows = (g.streams || [])
          .map((key) => (finance.streams.find((s) => s.key === key) || {}).days || 0)
          .filter((d) => d > 0);
        const longest = windows.length ? Math.max(...windows) : 0;
        if (g.days > 0) {
          says.push(
            longest - g.days > 1
              ? `${fmt(g.days, 0)}-${fmt(longest, 0)} days measured`
              : `${fmt(g.days, 0)} days measured`
          );
        }
        return (
          `<div class="row${g.key === "all" ? " sum" : ""}">` +
          `<div class="head">` +
          `<span class="what">${finEscape(g.name)}</span>` +
          `<span class="figure">${finInt(g.returned_chf)} / ${finInt(g.net)} ${cur}</span>` +
          `</div>` +
          `<div class="track"><span class="fill" style="width:${pct.toFixed(
            1
          )}%;min-width:${g.returned_chf > 0 ? 2 : 0}px"></span></div>` +
          `<div class="says">${says.join(" &middot; ")}</div>` +
          `</div>`
        );
      })
      .join("") +
    `</div>`;
}

// Recorded days are not elapsed days, and the quote's own promise is the
// one comparison worth a line above the fold. Everything else that used
// to be in two paragraphs here is in the table.
function finRenderNote() {
  const node = finEl("financeNote");
  if (!node) return;
  const m = finance.measured;
  const start = m.earning_since || m.first_record;
  const parts = [];
  if (start) {
    const missing =
      m.elapsed_days === null || m.elapsed_days === undefined
        ? 0
        : m.elapsed_days - m.recorded_days;
    parts.push(
      missing < 0.5
        ? `Measured from ${finDate(start)}, with no gaps.`
        : `${fmt(m.recorded_days, 0)} of the ${fmt(m.elapsed_days, 0)} days since ` +
            `${finDate(start)} are recorded; the other ${fmt(missing, 0)} are service ` +
            `downtime the installation went on saving through, so every total here is a floor.`
    );
  }
  const b = finance.benchmark;
  if (b && b.break_even_date) {
    parts.push(
      `The ${finEscape(b.label)} put the roof at ${fmt(b.payback_years, 0)} years &mdash; ` +
        `${finMonth(b.break_even_date)}. Breakdown and benchmark in the table.`
    );
  }
  node.innerHTML = parts.join(" ");
}

function finRenderBenchmark() {
  const node = finEl("financeBenchmark");
  if (!node) return;
  const b = finance.benchmark;
  if (!b) {
    node.textContent = "";
    return;
  }

  // Why the window exists, stated before any number from it. Under an
  // export ban the inverter is held to the house's own load: the moment
  // production would have passed the load and a full battery, it was
  // throttled. So those months record a permit, not a roof, and no ratio
  // against a predicted annual yield can be read off them - the size of
  // the error is not knowable, and nor is its sign.
  const excluded = b.excluded_days > 0.5;
  const prefix = excluded
    ? `<em>The first ${fmt(b.excluded_days, 0)} recorded days are excluded: with no feed-in ` +
      `permitted, the inverter was held to the house's own consumption, so the roof was never ` +
      `allowed to show its output and nothing can be concluded from what it produced. The ` +
      `savings from that time are real money and stay in the counter above.</em>`
    : "";

  if (!b.window_from) {
    node.innerHTML =
      prefix +
      ` No comparison with the ${finEscape(b.label)} is possible yet: set ` +
      `<code>EXPORT_ALLOWED_FROM</code> to the day feed-in was approved and the benchmark ` +
      `starts counting there.`;
    return;
  }

  if (!b.ready) {
    node.innerHTML =
      prefix +
      ` The comparison with the ${finEscape(b.label)} starts ${finDate(b.window_from)}: ` +
      `<strong>${fmt(b.window_days, 1)} of the ${fmt(b.min_days, 0)} days</strong> it needs ` +
      `before a ratio means anything.`;
    return;
  }

  // Two ratios, because they say different things and only together: the
  // money ratio is what the household gets, the production ratio is what
  // the roof does. A battery the quote did not include keeps more in the
  // house than its plan assumed, so the two can come apart.
  // The quote's expectation is its annual figure spread over the window's
  // own months, not divided by 365. A window of November against a twelfth
  // of a year would read as a fault in the roof rather than as winter.
  node.innerHTML =
    prefix +
    ` Against the ${finEscape(b.label)}, over the ${fmt(b.window_days, 0)} days since ` +
    `${finDate(b.window_from)} &mdash; ${fmt(100 * b.window_year_share, 0)}% of a year's sun, ` +
    `not ${fmt((100 * b.window_days) / 365, 0)}% of its days: ` +
    `<strong>${fmt(b.measured_chf, 0)} ${finance.currency} earned where its plan would have ` +
    `earned ${fmt(b.expected_chf_to_date_at_my_tariffs, 0)} at my tariffs ` +
    `(${fmt(b.return_pct, 0)}%), and ${fmt(b.measured_kwh_per_day, 1)} kWh a day produced ` +
    `where its yield over these months comes to ${fmt(b.expected_kwh_per_day, 1)} ` +
    `(${fmt(b.production_pct, 0)}%)</strong>. Its own figure, at its own ` +
    `${fmt(b.import_chf_per_kwh, 4)}/kWh, was ${fmt(b.expected_chf_to_date, 0)} ` +
    `${finance.currency}.`;
}

function finRenderTable() {
  const host = finEl("financeTable");
  if (!host) return;
  const inv = finance.investment;
  const rows = [
    ["Invoice total", finMoney(inv.invoice, 2), finance.known ? "" : "not entered yet"],
    ...(inv.extra
      ? [[finEscape(inv.extra_label), `+${finMoney(inv.extra, 2)}`, "paid outside the solar contract"]]
      : []),
    ["Grant received", `-${finMoney(inv.subsidy, 2)}`, "for the energy installation, so it comes off the invoice's side"],
    [
      "Tax deduction",
      `-${finMoney(inv.tax_benefit, 2)}`,
      inv.tax_rate === 1
        ? "entered as money, not as a deductible amount"
        : `${fmt(100 * inv.tax_rate, 0)}% of ${finMoney(inv.tax_deduction, 0)}`,
    ],
    ["<strong>Net investment</strong>", `<strong>${finMoney(inv.net, 2)}</strong>`, ""],
  ];
  // The tariff history, because a counter that runs for years is priced
  // by it and the rate is not one number.
  const hist = (finance.tariff && finance.tariff.history) || [];
  if (hist.length) {
    rows.push([
      "Tariff",
      hist.map((h) => `${fmt(h.import_per_kwh, 4)}`).join(" &rarr; "),
      `import, for ${hist.map((h) => h.year).join(" and ")} &mdash; each month is priced at that year's rate`,
    ]);
  }

  // Each payback with the streams that pay it off, so the rows above can
  // be checked against the money they were built from.
  for (const g of finance.groups || []) {
    rows.push([
      `<strong>${finEscape(g.name)}</strong>`,
      `<strong>${finMoney(g.returned_chf, 2)} / ${finMoney(g.net, 0)}</strong>`,
      g.break_even_date
        ? `${fmt(g.rate_chf_per_day, 2)}/day &rarr; ${finDate(g.break_even_date)}, from ${
            g.streams.join(", ") || "nothing yet"
          }`
        : `nothing measurable yet, from ${g.streams.join(", ") || "nothing yet"}`,
    ]);
  }

  const b = finance.benchmark;
  if (b) {
    rows.push([
      `<strong>${finEscape(b.label)}</strong>`,
      `<strong>${finMoney(b.net_chf, 2)}</strong>`,
      `net, at ${fmt(b.kwp, 1)} kWp`,
    ]);
    rows.push([
      "&nbsp;&nbsp;expected yield",
      `${fmt(b.kwh_per_year, 0)} kWh/year`,
      `${fmt(b.self_kwh_per_year, 0)} kWh used in the house, ${fmt(
        b.export_kwh_per_year,
        0
      )} kWh exported &mdash; spread over the months by a standard seasonal profile, the one assumption here not taken from the quote`,
    ]);
    rows.push([
      "&nbsp;&nbsp;expected return",
      `${fmt(b.chf_per_year, 0)} ${finance.currency}/year`,
      `at ${fmt(b.import_chf_per_kwh, 4)} import and ${fmt(
        b.export_chf_per_kwh,
        2
      )} export &mdash; the tariffs the promise was made at`,
    ]);
    rows.push([
      "&nbsp;&nbsp;the same plan, my tariffs",
      `${fmt(b.chf_per_year_at_my_tariffs, 0)} ${finance.currency}/year`,
      "what the quoted energy plan would earn here &mdash; the figure the counter is measured against",
    ]);
  }

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
    </table>`;
}

async function fetchFinance() {
  try {
    const res = await fetch("/api/finance");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    finance = await res.json();
    renderFinance();
  } catch (err) {
    const wrap = finEl("financeWrap");
    if (wrap) {
      wrap.innerHTML = `<div class="empty-state">Failed to load the finance figures (${err.message})</div>`;
    }
  }
}

// A few francs a day: this does not belong on the dashboard's 30-second
// clock. No resize hook either - the rows are HTML and the browser
// reflows them.
fetchFinance();
setInterval(() => fetchFinance(), 600000);

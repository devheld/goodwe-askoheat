// The pieces the d3 pages share: the tooltip, the number and money
// formatting, and the SVG scaffold. Extracted when the charging sessions
// moved from the energy page to the wallbox page and both pages needed the
// same twelve helpers.
//
// Loaded before the page's own script, and it expects a <div id="tooltip">
// in the markup. On the wallbox page app.js is on the same page and defines
// fmt() and cssVar() to the same contract - see the comment there about
// which one ends up in force.

// Prices are configuration, not measurement: they come from /api/energy or
// /api/live, which read them from the .env file. Until a page calls
// setTariff() money() renders zeros rather than a guessed rate.
const chartTariff = { currency: "CHF", import_per_kwh: 0, export_per_kwh: 0 };

function setTariff(tariff) {
  if (tariff) Object.assign(chartTariff, tariff);
}

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
  })} ${chartTariff.currency}`;
}

// Consumed kWh are priced at the import tariff even when they came off the
// roof: that is the purchase they replaced. Exported kWh are priced at the
// lower export tariff.
function rateFor(kind) {
  return kind === "export" ? chartTariff.export_per_kwh : chartTariff.import_per_kwh;
}

function cost(kwh, kind = "import") {
  return money((kwh || 0) * rateFor(kind));
}

function kwhWithCost(kwh, kind = "import", digits = 2) {
  return `${fmt(kwh, digits)} kWh (${cost(kwh, kind)})`;
}

const tooltip = document.getElementById("tooltip");

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

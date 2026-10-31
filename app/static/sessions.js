// Charging sessions, the charger's event log and the cross-check between
// the clamp meter and the charger's own registers - the wallbox page.
//
// None of it is read off the charger live: reading it while a car draws cuts
// the charging current (see the README), so the history comes from what was
// recorded. Since 31 Oct 2026 what was recorded is a measurement, from a
// clamp meter on the charger's feed; before that it is a reconstruction from
// the charger's counter, and every session says which it is.
//
// Needs chartkit.js and d3 before it.

const SESSION_DAYS = 30;
// Sessions end minutes apart at best, so this is about catching the one
// that finished while the page stayed open, not about being live.
const SESSION_REFRESH_MS = 5 * 60 * 1000;

function renderSessions(payload) {
  const container = document.getElementById("chartSessions");
  const table = document.getElementById("tableSessions");
  if (!container) return;
  const sessions = (payload.sessions || []).slice(0, 20);
  if (!sessions.length) {
    container.innerHTML =
      '<div class="empty-state">No charging session recorded yet - the page fills up once a car charges.</div>';
    table.innerHTML = "";
    return;
  }

  // Measured sessions wear the clamp's colour, reconstructed ones the
  // charger's own - the same pairing as the two lines in the power chart
  // above, so the page only has to teach it once.
  const barColor = (s) =>
    s.measured ? "--series-wallbox-meter" : "--series-wallbox";

  const rowH = 26;
  const width = chartWidth(container);
  const height = sessions.length * rowH + 30;
  // The date gutter and the value label are both text, so their room is
  // measured in characters rather than in a share of the width. At phone
  // width the full 150px gutter took 43% of the chart and the value label
  // ran off the right edge, so both get shorter instead of smaller.
  const narrow = width < 560;
  const padL = narrow ? 88 : 150;
  const padR = narrow ? 58 : 60;
  const padT = 8;
  const svg = newSvg(container, width, height);

  const x = d3
    .scaleLinear()
    .domain([0, d3.max(sessions, (s) => s.energy_kwh) * 1.1 || 1])
    .range([padL, width - padR]);

  sessions.forEach((s, i) => {
    const y = padT + i * rowH;
    const start = new Date(s.start);
    svg
      .append("text")
      .attr("x", padL - 10)
      .attr("y", y + rowH / 2 + 3)
      .attr("text-anchor", "end")
      .attr("font-size", 11)
      .attr("fill", cssVar("--text-secondary"))
      .text(
        narrow
          ? start.toLocaleString("en-GB", {
              day: "2-digit",
              month: "2-digit",
              hour: "2-digit",
              minute: "2-digit",
            })
          : start.toLocaleString("en-GB", {
              weekday: "short",
              day: "2-digit",
              month: "2-digit",
              hour: "2-digit",
              minute: "2-digit",
            })
      );

    const w = Math.max(2, x(s.energy_kwh) - padL);
    svg
      .append("rect")
      .attr("x", padL)
      .attr("y", y + 4)
      .attr("width", w)
      .attr("height", rowH - 10)
      .attr("rx", 4)
      .attr("fill", cssVar(barColor(s)))
      .on("mousemove", (event) =>
        showTip(
          tipRows(start.toLocaleString("en-GB"), [
            { color: barColor(s), label: "Charged", value: kwhWithCost(s.energy_kwh) },
            { label: "Duration", value: `${fmt(s.duration_min, 0)} min` },
            {
              label: s.measured ? "Peak" : "Peak (derived)",
              value: `${fmt(s.peak_kw, 1)} kW`,
            },
            {
              label: "Source",
              value: s.measured
                ? "Clamp meter on the feed"
                : "Charger's counter, placed by house load",
            },
          ]),
          event
        )
      )
      .on("mouseleave", hideTip);

    svg
      .append("text")
      .attr("x", padL + w + 8)
      .attr("y", y + rowH / 2 + 3)
      .attr("font-size", 11)
      .attr("fill", cssVar("--text-secondary"))
      // The cost is in the tooltip and in the table either way, so the
      // label that does not fit is the one that goes.
      .text(
        narrow
          ? `${fmt(s.energy_kwh, 1)} kWh`
          : `${fmt(s.energy_kwh, 1)} kWh (${cost(s.energy_kwh)})`
      );
  });

  table.innerHTML = `
    <table class="data">
      <thead><tr><th>Start</th><th>Duration</th><th>Energy</th><th>Cost</th><th>Peak</th><th>Source</th></tr></thead>
      <tbody>${(payload.sessions || [])
        .map(
          (s) => `<tr>
            <td>${new Date(s.start).toLocaleString("en-GB")}</td>
            <td>${fmt(s.duration_min, 0)} min</td>
            <td>${fmt(s.energy_kwh, 2)} kWh</td>
            <td>${cost(s.energy_kwh)}</td>
            <td>${fmt(s.peak_kw, 1)} kW</td>
            <td>${s.measured ? "Measured" : "Reconstructed"}</td>
          </tr>`
        )
        .join("")}</tbody>
    </table>`;
  // How much of this is measured and how much reconstructed, in the headline
  // rather than only in the table: a total that mixes the two should say so.
  const reconstructed = payload.sessions.length - (payload.measured_count || 0);
  document.getElementById("sessionNote").textContent =
    `Last ${payload.days} days - ${fmt(payload.total_kwh, 1)} kWh in ${
      payload.sessions.length
    } session(s) worth ${cost(payload.total_kwh)}. ` +
    (reconstructed === 0
      ? "All of it measured by the clamp meter on the charger's feed."
      : (payload.measured_count || 0) === 0
      ? "All of it reconstructed from the charger's counter, from before the clamp meter went in."
      : `${payload.measured_count} measured by the clamp meter (${fmt(
          payload.measured_kwh,
          1
        )} kWh), ${reconstructed} reconstructed from the charger's counter from before it went in.`);
}

// The clamp meter against the charger's own registers. Not a chart: three
// comparisons, each of which needs a sentence about what it does and does
// not establish, which is a table's job and not a bar's.
function renderCrosscheck(x) {
  const body = document.getElementById("crosscheckBody");
  if (!body) return;
  if (!x.metered_samples) {
    body.innerHTML =
      '<div class="empty-state">No clamp meter reading on the charger\'s feed yet.</div>';
    return;
  }

  const rows = [];

  // 1. Standby. The clamp resolves it; the register cannot.
  rows.push({
    what: "Standby, nothing plugged in",
    clamp: x.standby_kw === null ? "-" : `${fmt(x.standby_kw * 1000, 0)} W`,
    charger: "0.0 kW",
    says:
      x.standby_kw === null
        ? "Not yet observed."
        : `Agreed, as far as the charger can tell: ${fmt(
            x.standby_kw * 1000,
            0
          )} W is far below the 100 W step its power register works in, so 0.0 kW is the only answer it can give.`,
  });

  // 2. Power, where both were recorded on the same tick.
  rows.push({
    what: `Power, ${x.power_samples} sample(s) compared`,
    clamp: x.power_mean_diff_kw === null ? "-" : "reference",
    charger:
      x.power_mean_diff_kw === null
        ? "-"
        : `${x.power_mean_diff_kw > 0 ? "+" : ""}${fmt(x.power_mean_diff_kw * 1000, 0)} W mean`,
    says:
      x.power_samples === 0
        ? "The charger has not been read while the clamp was fitted."
        : `Worst single difference ${fmt(
            Math.abs(x.power_max_diff_kw) * 1000,
            0
          )} W. This only ever compares an idle charger - it is read at no other time - so it pins the zero point and nothing about the scale.`,
  });

  // 3. The two counters. The real check, and it needs a charge.
  const m = x.meter_counter;
  const c = x.charger_counter;
  rows.push({
    what: m ? `Energy over ${fmt(m.hours, 1)} h` : "Energy",
    clamp: m ? `${fmt(m.kwh, 3)} kWh` : "-",
    charger: c ? `${fmt(c.kwh, 1)} kWh` : "-",
    says:
      !m || !c
        ? "Needs both counters recorded at two points in time."
        : c.kwh === 0 && m.kwh < x.register_resolution_kwh
        ? `Consistent but uninformative: ${fmt(
            m.kwh * 1000,
            0
          )} Wh is below the charger's own 0.1 kWh resolution, so its counter could not have moved. This is the comparison that will settle the question, and it needs a charge.`
        : `Difference ${fmt(m.kwh - c.kwh, 2)} kWh, ${
            c.kwh ? fmt(((m.kwh - c.kwh) / c.kwh) * 100, 1) : "-"
          }% of the charger's own figure.`,
  });

  body.innerHTML = `
    <table class="data">
      <thead><tr><th>Quantity</th><th>Clamp meter</th><th>Charger</th><th>What that establishes</th></tr></thead>
      <tbody>${rows
        .map(
          (r) => `<tr>
            <td>${r.what}</td>
            <td>${r.clamp}</td>
            <td>${r.charger}</td>
            <td>${r.says}</td>
          </tr>`
        )
        .join("")}</tbody>
    </table>`;
}

function renderEvents(payload) {
  const list = document.getElementById("eventList");
  if (!list) return;
  const events = payload.events || [];
  if (!events.length) {
    list.innerHTML = '<li class="empty-state">No state change recorded yet.</li>';
    return;
  }
  list.innerHTML = events
    .map(
      (e) => `<li>
        <span class="when">${new Date(e.timestamp).toLocaleString("en-GB")}</span>
        <span class="kind">${e.event}</span>
        <span>${e.detail}</span>
      </li>`
    )
    .join("");
}

async function loadSessions() {
  try {
    const [sessions, events, crosscheck] = await Promise.all([
      fetch(`/api/wallbox/sessions?days=${SESSION_DAYS}`).then((r) => r.json()),
      fetch(`/api/wallbox/events?days=${SESSION_DAYS}&limit=50`).then((r) => r.json()),
      fetch(`/api/wallbox/crosscheck?days=${SESSION_DAYS}`).then((r) => r.json()),
    ]);
    lastPayloads = { runs: sessions, compare: crosscheck };
    renderSessions(sessions);
    renderEvents(events);
    renderCrosscheck(crosscheck);
  } catch (err) {
    document.getElementById("sessionNote").textContent =
      `Wallbox history unavailable (${err.message})`;
  }
}

// The prices come with the live payload, which app.js is already fetching -
// so the first draw waits for it instead of asking the service a third
// time for the same three numbers.
// Charts are sized to their container at draw time, so a rotated phone
// redraws. The payloads are kept rather than refetched.
let lastPayloads = null;
onViewportResize(() => {
  if (!lastPayloads) return;
  renderSessions(lastPayloads.runs);
  renderCrosscheck(lastPayloads.compare);
});

let sessionsStarted = false;
window.onLiveData = (latest) => {
  setTariff(latest.tariff);
  if (sessionsStarted) return;
  sessionsStarted = true;
  loadSessions();
  window.setInterval(loadSessions, SESSION_REFRESH_MS);
};

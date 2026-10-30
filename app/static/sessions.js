// Charging sessions and the charger's event log, on the wallbox page.
//
// Both are rebuilt from the recorded samples rather than read off the
// charger: reading it while a car draws cuts the charging current (see the
// README), so the history has to come from what was already recorded.
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

  const rowH = 26;
  const width = 960;
  const height = sessions.length * rowH + 30;
  const padL = 150, padR = 60, padT = 8;
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
      .text(start.toLocaleString("en-GB", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }));

    const w = Math.max(2, x(s.energy_kwh) - padL);
    svg
      .append("rect")
      .attr("x", padL)
      .attr("y", y + 4)
      .attr("width", w)
      .attr("height", rowH - 10)
      .attr("rx", 4)
      .attr("fill", cssVar("--series-wallbox"))
      .on("mousemove", (event) =>
        showTip(
          tipRows(start.toLocaleString("en-GB"), [
            { color: "--series-wallbox", label: "Charged", value: kwhWithCost(s.energy_kwh) },
            { label: "Duration", value: `${fmt(s.duration_min, 0)} min` },
            { label: "Peak", value: `${fmt(s.peak_kw, 1)} kW` },
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
      .text(`${fmt(s.energy_kwh, 1)} kWh (${cost(s.energy_kwh)})`);
  });

  table.innerHTML = `
    <table class="data">
      <thead><tr><th>Start</th><th>Duration</th><th>Energy</th><th>Cost</th><th>Peak</th></tr></thead>
      <tbody>${(payload.sessions || [])
        .map(
          (s) => `<tr>
            <td>${new Date(s.start).toLocaleString("en-GB")}</td>
            <td>${fmt(s.duration_min, 0)} min</td>
            <td>${fmt(s.energy_kwh, 2)} kWh</td>
            <td>${cost(s.energy_kwh)}</td>
            <td>${fmt(s.peak_kw, 1)} kW</td>
          </tr>`
        )
        .join("")}</tbody>
    </table>`;
  document.getElementById("sessionNote").textContent =
    `Last ${payload.days} days, rebuilt from the recorded samples - ${fmt(payload.total_kwh, 1)} kWh in ${
      payload.sessions.length
    } session(s) worth ${cost(payload.total_kwh)}.`;
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
    const [sessions, events] = await Promise.all([
      fetch(`/api/wallbox/sessions?days=${SESSION_DAYS}`).then((r) => r.json()),
      fetch(`/api/wallbox/events?days=${SESSION_DAYS}&limit=50`).then((r) => r.json()),
    ]);
    renderSessions(sessions);
    renderEvents(events);
  } catch (err) {
    document.getElementById("sessionNote").textContent =
      `Wallbox history unavailable (${err.message})`;
  }
}

// The prices come with the live payload, which app.js is already fetching -
// so the first draw waits for it instead of asking the service a third
// time for the same three numbers.
let sessionsStarted = false;
window.onLiveData = (latest) => {
  setTariff(latest.tariff);
  if (sessionsStarted) return;
  sessionsStarted = true;
  loadSessions();
  window.setInterval(loadSessions, SESSION_REFRESH_MS);
};

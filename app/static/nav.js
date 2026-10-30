// The navigation shared by every page. One file so the icons and the
// behaviour exist once; each page only places an empty <nav data-nav>.
//
// The icons are drawn here rather than loaded: four line drawings are
// smaller than one icon-font request, they inherit the accent colour
// through currentColor, and nothing else on the page has to wait for them.

const NAV_ITEMS = [
  {
    href: "/",
    label: "Dashboard",
    accent: "--nav-dash",
    // A house with the live trace inside it.
    icon: `<path d="M3.2 11.2 12 4.2l8.8 7"/><path d="M5.6 9.8V19.6h12.8V9.8"/>
           <path d="M8.4 16.1l2.3-2.7 1.8 1.6 2.9-3.4"/>`,
  },
  {
    href: "/asko",
    label: "AskoHeat",
    accent: "--nav-asko",
    // The tank, the water in it, and the element hanging into it.
    icon: `<rect x="5.6" y="4.4" width="12.8" height="15.4" rx="4"/>
           <path d="M8.3 15.1c1.2-1.1 2.4-1.1 3.6 0s2.4 1.1 3.6 0"/>
           <path d="M12 4.4v6.6"/><path d="M10.2 11h3.6"/>`,
  },
  {
    href: "/wallbox",
    label: "Wallbox",
    accent: "--nav-wallbox",
    // The box on the wall, the cable hanging off it, the bolt on its face.
    icon: `<rect x="3.6" y="3.4" width="9" height="17.2" rx="2.6"/>
           <path d="M6.1 7.2h4"/>
           <path d="M12.6 17.4c3 0 4.4-1.1 4.4-3.3V8.9"/>
           <rect x="15.4" y="5.2" width="3.4" height="3.6" rx="1.2"/>
           <path d="M9.4 10.6 7.4 14.3h2.1l-.5 2.6 2.2-3.8H9.1z" fill="currentColor" stroke="none"/>`,
  },
  {
    href: "/energy",
    label: "Energy",
    accent: "--nav-energy",
    // Three bars on a baseline: what the page is made of.
    icon: `<path d="M4 19.6h16"/><path d="M7.4 19.6v-4.4"/>
           <path d="M12 19.6v-8.2"/><path d="M16.6 19.6v-11.4"/>`,
  },
];

function navIcon(paths) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"
    aria-hidden="true">${paths}</svg>`;
}

// "/" matches only "/": every page is a prefix of it otherwise.
function isCurrent(href, path) {
  return href === "/" ? path === "/" : path === href || path.startsWith(href + "/");
}

function renderNav() {
  const host = document.querySelector("[data-nav]");
  if (!host) return;
  const path = window.location.pathname.replace(/\/+$/, "") || "/";
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  host.className = "appnav" + (reduced ? "" : " animate");
  host.setAttribute("aria-label", "Sections");
  host.innerHTML = NAV_ITEMS.map((item, i) => {
    const current = isCurrent(item.href, path);
    return `<a class="chip" href="${item.href}" data-accent="${item.accent}"
      style="--accent: var(${item.accent}); animation-delay: ${i * 40}ms"
      ${current ? 'aria-current="page"' : ""}>
      ${navIcon(item.icon)}<span>${item.label}</span>
    </a>`;
  }).join("");

  for (const chip of host.querySelectorAll(".chip")) {
    chip.addEventListener("click", (event) => onChipClick(event, chip, reduced));
  }
}

// A ripple from where the finger landed, and the navigation held back just
// long enough to see it start. 150ms is below the ~200ms that reads as a
// delay, so the page still feels immediate.
const NAV_HOLD_MS = 150;

function onChipClick(event, chip, reduced) {
  if (reduced || event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) {
    return;
  }
  const rect = chip.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  // Big enough to cover the chip from wherever it started.
  const size = 2 * Math.max(x, rect.width - x, y, rect.height - y);

  const ripple = document.createElement("span");
  ripple.className = "ripple";
  ripple.style.width = ripple.style.height = `${size}px`;
  ripple.style.left = `${x - size / 2}px`;
  ripple.style.top = `${y - size / 2}px`;
  chip.appendChild(ripple);
  ripple.addEventListener("animationend", () => ripple.remove());

  // The page you are already on: let the ripple play, do not reload.
  if (chip.getAttribute("aria-current") === "page") {
    event.preventDefault();
    return;
  }
  event.preventDefault();
  window.setTimeout(() => {
    window.location.href = chip.getAttribute("href");
  }, NAV_HOLD_MS);
}

renderNav();

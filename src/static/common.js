// Helpers shared by all pages.

// The navigation bar at the top of every page.
const PAGES = [
  ["/", "Chargers"], ["/tags", "Tags"], ["/energy", "Energy"],
  ["/sessions", "Sessions"], ["/settings", "Settings"],
];

function renderNav() {
  const here = location.pathname.replace(/\/index\.html$/, "/");
  document.body.insertAdjacentHTML("afterbegin", `<nav class="topnav" aria-label="Pages"><div>
    <span class="brand">simple-ocpp</span>
    ${PAGES.map(([href, label]) =>
      `<a href="${href}" ${href === here ? 'aria-current="page"' : ""}>${label}</a>`).join("")}
  </div></nav>`);
}
document.addEventListener("DOMContentLoaded", renderNav);

const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const kwh = wh => `${(wh / 1000).toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} kWh`;

// Sets innerHTML only when it changed, so the 3-second refresh doesn't reset
// a dropdown the user is about to open, or move focus.
function setHtml(el, html) {
  if (el.dataset.html !== html) {
    el.innerHTML = html;
    el.dataset.html = html;
  }
}

// FastAPI errors look like {detail: "text"} or, for invalid input,
// {detail: [{loc: [...], msg: "text"}, ...]}.
function errorText(data, status) {
  const d = data.detail ?? data.error ?? data.message;
  if (Array.isArray(d)) return d.map(e => `${e.loc?.at(-1) ?? ""}: ${e.msg}`).join("; ");
  return d || `HTTP ${status}`;
}

async function api(method, url, body) {
  const res = await fetch(url, {
    method, headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(errorText(data, res.status));
  return data;
}

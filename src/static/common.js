// Helpers shared by all pages.

// The navigation bar at the top of every page.
const PAGES = [
  ["/", "Chargers"], ["/tags", "Tags"], ["/energy", "Energy"],
  ["/sessions", "Sessions"], ["/prices", "Prices"], ["/logs", "Logs"], ["/settings", "Settings"],
];

async function renderNav() {
  if (document.body.dataset.nav === "none") return;
  const here = location.pathname.replace(/\/index\.html$/, "/");
  document.body.insertAdjacentHTML("afterbegin", `<nav class="topnav" aria-label="Pages"><div>
    <span class="brand">simple-ocpp</span>
    ${PAGES.map(([href, label]) =>
      `<a href="${href}" ${href === here ? 'aria-current="page"' : ""}>${label}</a>`).join("")}
  </div></nav>`);
  const status = await api("GET", "/api/auth").catch(() => null);
  if (status?.enabled) addLogout();
}

function addLogout() {
  if (document.getElementById("logout")) return;
  document.querySelector(".topnav > div")?.insertAdjacentHTML("beforeend",
    `<button class="logout" id="logout">Log out</button>`);
  document.getElementById("logout")?.addEventListener("click", async () => {
    await api("POST", "/api/logout").catch(() => {});
    location.href = "/login";
  });
}
document.addEventListener("DOMContentLoaded", renderNav);

// With a password set, a call answered 401 means the login is missing or has
// expired: go to the login page, and come back here afterwards.
const originalFetch = window.fetch.bind(window);
window.fetch = async (input, init) => {
  const res = await originalFetch(input, init);
  const url = typeof input === "string" ? input : input.url;
  if (res.status === 401 && !url.startsWith("/api/login") && location.pathname !== "/login") {
    location.href = `/login?next=${encodeURIComponent(location.pathname + location.search)}`;
  }
  return res;
};

const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// What to call a charger on the page: its friendly name, else its ID.
const chargerName = c => c.name || c.id;

// Localisation, from the Settings page. "browser" follows the browser's language.
let localisation = { time_format: "browser", decimal_separator: "browser", currency: "€" };

// Pages wait for this before their first draw. The login page needs none.
const localisationReady = new Promise(resolve => {
  document.addEventListener("DOMContentLoaded", async () => {
    if (document.body.dataset.nav !== "none") {
      try { localisation = await api("GET", "/api/localisation"); } catch { /* keep defaults */ }
    }
    resolve();
  });
});

function formatNumber(value, options = {}, config = localisation) {
  const parts = new Intl.NumberFormat(undefined, options).formatToParts(value);
  const decimal = config.decimal_separator;
  if (decimal === "browser") return parts.map(p => p.value).join("");
  const group = decimal === "," ? "." : ",";
  return parts.map(p => p.type === "decimal" ? decimal : p.type === "group" ? group : p.value).join("");
}

// The separator numbers are shown with, for CSV downloads ("." or ",").
function csvDecimal() {
  const parts = new Intl.NumberFormat(undefined).formatToParts(1.5);
  const shown = localisation.decimal_separator === "browser"
    ? parts.find(p => p.type === "decimal")?.value : localisation.decimal_separator;
  return shown === "," ? "," : ".";
}

function timeOptions(config = localisation) {
  return config.time_format === "browser" ? {}
    : { hourCycle: config.time_format === "12h" ? "h12" : "h23" };
}
const formatDateTime = (date, config = localisation) =>
  new Date(date).toLocaleString(undefined, timeOptions(config));
const formatTime = (date, config = localisation) =>
  new Date(date).toLocaleTimeString(undefined, timeOptions(config));

// A cost such as "€ 12.34"; "—" when there is none.
const money = (value, currency = localisation.currency) => value == null ? "—"
  : `${currency}\u00a0${formatNumber(value, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const kwh = wh => `${formatNumber(wh / 1000, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} kWh`;

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

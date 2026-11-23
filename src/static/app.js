// SPDX-FileCopyrightText: 2026 Jef Steelant
// SPDX-License-Identifier: GPL-3.0-only

// Script for the Chargers, Tags, Energy and Sessions pages. Each page has
// only some of the elements below; parts whose element is missing are skipped.

const $ = id => document.getElementById(id);

const LOCK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>';
const UNLOCK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.5-2"/></svg>';

const STATUS_CLASS = {
  Available: "ok", Preparing: "warn", Charging: "ok", SuspendedEV: "warn",
  SuspendedEVSE: "warn", Finishing: "warn", Reserved: "warn",
  Unavailable: "bad", Faulted: "bad",
};
let tags = [];               // registered RFID tags from the server
let groups = [];             // tag groups from the server
const SESSION_LIMIT = 20;    // sessions per page
const NO_STOP_RECEIVED = "NoStopReceived";  // closed by the server; see store.py
const findTag = id => tags.find(t => t.id_tag.toUpperCase() === String(id).toUpperCase());
// A tag outside the Tags page: its name (code on hover), or the code if unnamed.
const tagLabel = (name, id) => name
  ? `<span class="tagname" title="${esc(id)}">${esc(name)}</span>`
  : `<code class="rfid">${esc(id || "—")}</code>`;
const pending = new Set();   // charger ids with a request in flight
const messages = {};         // last request result per charger

function ago(iso) {
  if (!iso) return "never";
  const s = Math.round((Date.now() - new Date(iso)) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return formatDateTime(iso);
}

const power = w => w >= 1000
  ? `${formatNumber(w / 1000, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} kW`
  : `${Math.round(w)} W`;

// The sessions in progress on a charger: who, how long, how much so far.
function liveSessions(c) {
  const several = Object.keys(c.connectors).filter(id => id !== "0").length > 1 || c.active_sessions.length > 1;
  return c.active_sessions.map(s => {
    const status = c.connectors[s.connector_id]?.status;
    const pill = !c.connected ? `<span class="pill idle">In progress</span>`
      : status && status !== "Charging" ? `<span class="pill ${STATUS_CLASS[status] || "idle"}">${esc(status)}</span>`
      : `<span class="pill ok">Charging</span>`;
    const who = s.tag_name || findTag(s.id_tag)?.name;
    const stat = (label, value) => `<div><span>${label}</span><b>${value}</b></div>`;
    return `<div class="live">
      <div class="live-head">${pill}
        ${tagLabel(who, s.id_tag)}
        ${several ? `<span class="sub">connector ${esc(s.connector_id)}</span>` : ""}
      </div>
      <div class="live-stats">
        ${stat("Duration", esc(duration(s.started_at)))}
        ${stat("Energy", s.energy_wh == null ? "—" : esc(kwh(s.energy_wh)))}
        ${s.cost == null ? "" : stat("Cost", esc(money(s.cost, s.currency)))}
        ${stat("Power", s.power_w == null ? "—" : esc(power(s.power_w)))}
      </div>
      ${c.connected ? "" : `<div class="sub">The charger is offline; energy and power are unknown until it reconnects.</div>`}
    </div>`;
  }).join("");
}

function connectorRows(c) {
  const ids = Object.keys(c.connectors).filter(id => id !== "0");
  const rows = (ids.length ? ids : Object.keys(c.connectors)).map(id => {
    const k = c.connectors[id];
    const err = k.error_code && k.error_code !== "NoError" ? ` · ${esc(k.error_code)}` : "";
    const label = id === "0" ? "Charger" : `Connector ${esc(id)}`;
    const pill = k.status
      ? `<span class="pill ${STATUS_CLASS[k.status] || "idle"}">${esc(k.status)}</span>`
      : `<span class="sub">status unknown</span>`;
    const meter = [];
    if (k.power_w != null) meter.push(power(k.power_w));
    if (k.energy_wh != null) meter.push(`${formatNumber(k.energy_wh / 1000, { maximumFractionDigits: 1 })} kWh total`);
    const meterLine = meter.length ? `<div class="sub">${meter.join(" · ")}</div>` : "";
    return `<dt>${label}</dt><dd>${pill}${err}${meterLine}</dd>`;
  });
  return rows.join("") || `<dt>Status</dt><dd class="sub">${c.connected ? "No status reported yet" : "—"}</dd>`;
}

function lockInfo(c) {
  // What the charger reported, falling back to the stored choice when offline/unknown.
  const reported = c.lock_config;
  const locked = reported === "unknown" ? c.desired_locked : reported === "locked";
  let note = "";
  if (!c.connected) note = " (saved, offline)";
  else if (reported === "unknown") note = " (not confirmed)";
  else if ((reported === "locked") !== c.desired_locked) note = " (pending)";
  return { locked, label: (locked ? "Locked" : "Unlocked") + note };
}

function tagPill(t) {
  if (!t) return `<span class="pill bad">Unknown</span>`;
  return t.authorized
    ? `<span class="pill ok">Authorized</span>`
    : `<span class="pill bad">Blocked</span>`;
}

function rfidInfo(c) {
  if (!c.last_rfid) return `<span class="sub">none read yet</span>`;
  const t = findTag(c.last_rfid);
  const register = t ? "" :
    ` <button class="small" data-tag-action="register" data-tag="${esc(c.last_rfid)}">Register</button>`;
  return `${tagLabel(t?.name, c.last_rfid)} ${tagPill(t)}${register}
    <div class="sub">${ago(c.last_rfid_at)}</div>`;
}

function groupSelect(t) {
  const options = [`<option value="">— none —</option>`, ...groups.map(g =>
    `<option value="${esc(g.name)}" ${g.name === t.parent_id_tag ? "selected" : ""}>${esc(g.name)}</option>`)];
  return `<select data-tag-group="${esc(t.id_tag)}" aria-label="Group of ${esc(t.name || t.id_tag)}">${options.join("")}</select>`;
}

function renderTags() {
  const el = document.getElementById("tags");
  if (el.contains(document.activeElement) && document.activeElement.tagName === "SELECT") return;
  if (!tags.length) {
    setHtml(el, `<div class="empty" style="padding:30px 0">No tags registered yet. Scan a card at a charger, then click <b>Register</b> next to it on the <a href="/">Chargers</a> page.</div>`);
    return;
  }
  setHtml(el, `<table>
    <tr><th>Name</th><th>Tag</th><th>Group</th><th>Status</th><th></th></tr>
    ${tags.map(t => `<tr>
      <td class="tagname">${t.name ? esc(t.name) : `<span class="sub">unnamed</span>`}</td>
      <td><code class="rfid">${esc(t.id_tag)}</code></td>
      <td>${groupSelect(t)}</td>
      <td>${tagPill(t)}</td>
      <td class="actions">
        <button class="small secondary" data-tag-action="rename" data-tag="${esc(t.id_tag)}">Rename</button>
        <button class="small ${t.authorized ? "secondary" : ""}" data-tag-action="${t.authorized ? "deauthorize" : "authorize"}"
          data-tag="${esc(t.id_tag)}">${t.authorized ? "Deauthorize" : "Authorize"}</button>
        <button class="small danger" data-tag-action="remove" data-tag="${esc(t.id_tag)}">Remove</button>
      </td>
    </tr>`).join("")}
  </table>`);
}

function renderGroups() {
  const el = document.getElementById("groups");
  if (!groups.length) {
    setHtml(el, `<div class="empty" style="padding:30px 0">No groups yet. Tags in the same group can stop each other's charge sessions.</div>`);
    return;
  }
  setHtml(el, `<table>
    <tr><th>Group</th><th>Tags</th><th></th></tr>
    ${groups.map(g => {
      const members = tags.filter(t => t.parent_id_tag === g.name);
      const list = members.map(t => t.name ? `<span class="tagname">${esc(t.name)}</span>` : `<code class="rfid">${esc(t.id_tag)}</code>`);
      return `<tr>
        <td class="tagname">${esc(g.name)}</td>
        <td>${list.length ? list.join(", ") : `<span class="sub">no tags</span>`}</td>
        <td class="actions">
          <button class="small danger" data-group-action="delete" data-group="${esc(g.name)}">Delete</button>
        </td>
      </tr>`;
    }).join("")}
  </table>`);
}

function duration(fromIso, toIso) {
  const min = Math.max(0, Math.round((new Date(toIso ?? Date.now()) - new Date(fromIso)) / 60000));
  return min < 60 ? `${min} min` : `${Math.floor(min / 60)} h ${String(min % 60).padStart(2, "0")} min`;
}

function sessionStatus(s) {
  if (!s.accepted) return `<span class="pill bad">Refused</span>`;
  if (s.stopped_at == null) return `<span class="pill ok">Charging</span>`;
  if (s.stop_reason === NO_STOP_RECEIVED) {
    return `<span class="pill idle">Ended</span> <span class="sub">no stop received; energy estimated</span>`;
  }
  const notes = s.stop_reason ? [esc(s.stop_reason)] : [];
  if (s.stop_id_tag && s.stop_id_tag.toUpperCase() !== s.id_tag.toUpperCase()) {
    notes.push(`stopped by ${esc(findTag(s.stop_id_tag)?.name || s.stop_id_tag)}`);
  }
  return `<span class="pill idle">Done</span>` +
    (notes.length ? ` <span class="sub">${notes.join(" · ")}</span>` : "");
}

function sessionEnergy(s, chargers) {
  if (s.energy_wh != null) return kwh(s.energy_wh);
  // Running: compare the connector's latest meter reading with the start.
  const c = chargers.find(c => c.id === s.charger_id);
  const now = c?.connected ? c.connectors[s.connector_id]?.energy_wh : null;
  return now != null && s.accepted ? `<span class="sub">${kwh(now - s.meter_start_wh)} so far</span>` : "—";
}

function renderSessions(sessions, chargers) {
  const el = document.getElementById("sessions");
  if (!sessions.length) {
    const text = sessionView.preset === "all" ? "No charge sessions yet." : "No charge sessions in this period.";
    el.innerHTML = `<div class="empty" style="padding:30px 0">${text}</div>`;
    return;
  }
  el.innerHTML = `<table>
    <tr><th>Started</th><th>Charger</th><th>Tag</th><th class="num">Duration</th><th class="num">Energy</th>${hasTariff ? `<th class="num">Cost</th>` : ""}<th>Status</th></tr>
    ${sessions.map(s => {
      const name = s.tag_name || findTag(s.id_tag)?.name;
      const charger = chargers.find(c => c.id === s.charger_id);
      return `<tr>
        <td>${esc(formatDateTime(s.started_at))}</td>
        <td><span title="${esc(s.charger_id)}">${esc(charger?.name || s.charger_id)}</span>${s.several_connectors ? ` <span class="sub">#${esc(s.connector_id)}</span>` : ""}</td>
        <td>${tagLabel(name, s.id_tag)}</td>
        <td class="num">${s.accepted && s.stop_reason !== NO_STOP_RECEIVED ? duration(s.started_at, s.stopped_at) : "—"}</td>
        <td class="num">${sessionEnergy(s, chargers)}</td>
        ${hasTariff ? `<td class="num">${esc(money(s.cost, currency))}</td>` : ""}
        <td>${sessionStatus(s)}</td>
      </tr>`;
    }).join("")}
  </table>`;
}

// --- Sessions: paging and period filter ---------------------------------------

const sessionView = { page: 0, preset: "all", total: 0 };

// [since, until) as local-midnight Dates for the chosen period; null = open.
function sessionRange() {
  const now = new Date(), y = now.getFullYear(), m = now.getMonth(), d = now.getDate();
  const day = (yy, mm, dd) => new Date(yy, mm, dd);
  switch (sessionView.preset) {
    case "today": return [day(y, m, d), day(y, m, d + 1)];
    case "week": return [day(y, m, d - 6), day(y, m, d + 1)];
    case "month": return [day(y, m, 1), day(y, m + 1, 1)];
    case "lastmonth": return [day(y, m - 1, 1), day(y, m, 1)];
    case "year": return [day(y, 0, 1), day(y + 1, 0, 1)];
    case "custom": {
      const from = $("session-from").value, to = $("session-to").value;
      const parse = v => { const [yy, mm, dd] = v.split("-").map(Number); return day(yy, mm - 1, dd); };
      const until = to ? parse(to) : null;
      if (until) until.setDate(until.getDate() + 1);  // "to" is inclusive
      return [from ? parse(from) : null, until];
    }
    default: return [null, null];
  }
}

async function fetchSessions() {
  const [since, until] = sessionRange();
  const filter = new URLSearchParams();
  if (since) filter.set("since", since.toISOString());
  if (until) filter.set("until", until.toISOString());
  const page = new URLSearchParams({ limit: SESSION_LIMIT, offset: sessionView.page * SESSION_LIMIT });
  for (const [k, v] of filter) page.set(k, v);
  const [res, summary] = await Promise.all([fetch(`/api/sessions?${page}`), api("GET", `/api/sessions/summary?${filter}`)]);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return { list: await res.json(), total: Number(res.headers.get("X-Total-Count") ?? 0), summary, filter };
}

function renderSessionSummary({ summary, filter }) {
  const parts = [`<b>${summary.count}</b> session${summary.count === 1 ? "" : "s"}`];
  if (summary.count) parts.push(`<b>${esc(kwh(summary.energy_wh))}</b>`);
  if (summary.count && summary.cost != null) parts.push(`<b>${esc(money(summary.cost, summary.currency))}</b>`);
  $("session-summary").innerHTML = parts.join(" · ");
  const csv = new URLSearchParams(filter);
  csv.set("decimal", csvDecimal());
  $("session-csv").href = `/api/sessions.csv?${csv}`;
}

function renderPager(shown) {
  const { page, total } = sessionView;
  const first = page * SESSION_LIMIT;
  $("session-count").textContent = total
    ? `${first + 1}–${first + shown} of ${total} session${total === 1 ? "" : "s"}` : "";
  $("session-prev").disabled = page === 0;
  $("session-next").disabled = first + shown >= total;
}

function setUpSessions() {
  loadSettings();
  const changed = () => { sessionView.page = 0; refresh(); };
  $("session-period").addEventListener("change", e => {
    sessionView.preset = e.target.value;
    $("session-custom").hidden = sessionView.preset !== "custom";
    changed();
  });
  $("session-from").addEventListener("change", changed);
  $("session-to").addEventListener("change", changed);
  $("session-prev").addEventListener("click", () => { sessionView.page--; refresh(); });
  $("session-next").addEventListener("click", () => { sessionView.page++; refresh(); });
}

function card(c) {
  const busy = pending.has(c.id) || c.lock_busy;
  const { locked, label } = lockInfo(c);
  const action = c.desired_locked ? "unlock" : "lock";
  const msg = messages[c.id] ?? c.last_message;
  return `
  <section class="card ${c.connected ? "" : "offline"}">
    <div class="top">
      <div>
        <div class="name-row">
          <div class="name">${esc(chargerName(c))}</div>
          <button class="small secondary" data-rename="${esc(c.id)}" data-name="${esc(c.name)}"
            aria-label="Rename ${esc(chargerName(c))}">Rename</button>
        </div>
        ${c.name ? `<div class="sub">ID: <code>${esc(c.id)}</code></div>` : ""}
        <div class="sub">${esc([c.vendor, c.model].filter(Boolean).join(" · ") || "Unknown model")}</div>
      </div>
      <span class="pill ${c.connected ? "ok" : "idle"}">${c.connected ? "Online" : "Offline"}</span>
    </div>
    <dl>
      ${connectorRows(c)}
      <dt>Last seen</dt><dd>${ago(c.last_seen)}</dd>
      <dt>Last RFID</dt><dd>${rfidInfo(c)}</dd>
    </dl>
    ${liveSessions(c)}
    <div class="lockrow">
      <span class="lockstate" style="color: var(${locked ? "--ok" : "--warn"})">
        ${locked ? LOCK_ICON : UNLOCK_ICON}${esc(label)}
      </span>
      <button class="${action === "lock" ? "" : "secondary"}" data-id="${esc(c.id)}"
        data-action="${action}" ${busy ? "disabled" : ""}>
        ${busy ? "Working…" : action === "lock" ? "Lock" : "Unlock"}
      </button>
    </div>
    <label class="toggle">
      <input type="checkbox" data-plugin-lock="${esc(c.id)}" ${c.lock_on_plugin ? "checked" : ""}>
      Lock cable when plugged in
    </label>
    <div class="msg">${esc(msg)}</div>
    ${c.connected ? "" : `<div class="remove-row">
      <button class="small danger" data-remove="${esc(c.id)}" data-label="${esc(chargerName(c))}">Remove charger</button></div>`}
  </section>`;
}

// Fetches what this page shows and redraws it. Calls can overlap (the timer,
// a button, a filter change); only the newest one may draw.
let refreshCount = 0;

async function refresh() {
  await localisationReady;
  const call = ++refreshCount;
  const list = $("list");
  const needTags = Boolean(list || $("tags") || $("sessions"));
  const get = async (url, wanted = true) => {
    if (!wanted) return null;
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  };
  try {
    const [chargers, tagList, groupList, sessions] = await Promise.all([
      get("/api/chargers"), get("/api/tags", needTags), get("/api/groups", Boolean($("groups"))),
      $("sessions") ? fetchSessions() : null,
    ]);
    if (call !== refreshCount) return;  // a newer refresh has started
    if (tagList) tags = tagList;
    if (groupList) groups = groupList;
    if ($("tags")) renderTags();
    if ($("groups")) renderGroups();
    if (sessions) {
      // A filter or deletion can leave us past the last page: go back.
      if (!sessions.list.length && sessionView.page > 0) { sessionView.page = 0; return refresh(); }
      sessionView.total = sessions.total;
      renderSessionSummary(sessions);
      renderSessions(sessions.list, chargers);
      renderPager(sessions.list.length);
    }
    if ($("energy")) {
      updateEnergyChargers(chargers);
      loadEnergy();
    }
    if (list) list.innerHTML = chargers.length
      ? chargers.map(card).join("")
      : `<div class="empty">No chargers have connected yet.<br>Point a charger at <code>ws://&lt;this-host&gt;:9000/&lt;charger-id&gt;</code>.</div>`;
    $("updated").textContent = "Updated " + formatTime(new Date());
  } catch (e) {
    document.getElementById("updated").innerHTML =
      `<span class="pill bad">Server unreachable</span>`;
  }
}

$("list")?.addEventListener("click", async e => {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  const { id, action } = btn.dataset;
  pending.add(id);
  messages[id] = action === "lock" ? "Locking…" : "Unlocking…";
  refresh();
  try {
    const res = await fetch(`/api/chargers/${encodeURIComponent(id)}/${action}`, { method: "POST" });
    const body = await res.json();
    messages[id] = res.ok || body.message ? body.message : errorText(body, res.status);
  } catch (err) {
    messages[id] = "Request failed: " + err.message;
  } finally {
    pending.delete(id);
    refresh();
    setTimeout(() => { delete messages[id]; }, 10000);
  }
});

$("list")?.addEventListener("click", async e => {
  const btn = e.target.closest("button[data-rename]");
  if (!btn) return;
  const id = btn.dataset.rename;
  const name = prompt(`Name for charger ${id} (leave empty to show its ID):`, btn.dataset.name);
  if (name === null) return;
  try {
    await api("POST", `/api/chargers/${encodeURIComponent(id)}`, { name });
  } catch (err) {
    alert("Not renamed: " + err.message);
  }
  refresh();
});

// Removing an offline charger: the dialog asks whether to keep its history.
$("list")?.addEventListener("click", e => {
  const btn = e.target.closest("button[data-remove]");
  if (!btn) return;
  const dialog = $("remove-dialog");
  dialog.dataset.charger = btn.dataset.remove;
  $("remove-name").textContent = btn.dataset.label;
  dialog.showModal();
});

$("remove-dialog")?.addEventListener("close", async e => {
  const dialog = e.target, choice = dialog.returnValue;
  if (choice !== "keep" && choice !== "delete") return;
  const id = dialog.dataset.charger;
  try {
    await api("DELETE", `/api/chargers/${encodeURIComponent(id)}?delete_history=${choice === "delete"}`);
  } catch (err) {
    alert("Not removed: " + err.message);
  }
  refresh();
});

$("list")?.addEventListener("change", async e => {
  const id = e.target.dataset.pluginLock;
  if (id === undefined) return;
  try {
    await api("POST", `/api/chargers/${encodeURIComponent(id)}`, { lock_on_plugin: e.target.checked });
  } catch (err) {
    alert("Failed: " + err.message);
  }
  refresh();
});

document.addEventListener("click", async e => {
  const btn = e.target.closest("button[data-tag-action]");
  if (!btn) return;
  const { tagAction, tag } = btn.dataset;
  const url = `/api/tags/${encodeURIComponent(tag)}`;
  const existing = findTag(tag);
  try {
    if (tagAction === "register") {
      const name = prompt(`Name for RFID tag ${tag}:`, "");
      if (name === null) return;
      await api("POST", "/api/tags", { id_tag: tag, name, authorized: true });
    } else if (tagAction === "rename") {
      const name = prompt(`New name for RFID tag ${tag}:`, existing?.name ?? "");
      if (name === null) return;
      await api("POST", url, { name });
    } else if (tagAction === "authorize" || tagAction === "deauthorize") {
      await api("POST", url, { authorized: tagAction === "authorize" });
    } else if (tagAction === "remove") {
      const label = existing?.name ? `${existing.name} (${tag})` : tag;
      if (!confirm(`Remove RFID tag ${label}? It will no longer be able to start charging.`)) return;
      await api("DELETE", url);
    }
  } catch (err) {
    alert("Failed: " + err.message);
  }
  refresh();
});

$("tags")?.addEventListener("change", async e => {
  const tag = e.target.dataset.tagGroup;
  if (tag === undefined) return;
  try {
    await api("POST", `/api/tags/${encodeURIComponent(tag)}`, { parent_id_tag: e.target.value });
  } catch (err) {
    alert("Failed: " + err.message);
  }
  e.target.blur();  // let the refresh redraw the table again
  refresh();
});

document.addEventListener("click", async e => {
  const btn = e.target.closest("button[data-group-action]");
  if (!btn) return;
  try {
    if (btn.dataset.groupAction === "add") {
      const name = prompt("Name of the new group (max 20 characters).\n" +
        "Any tag in a group can stop a charge session started by another tag in the same group.");
      if (name === null || !name.trim()) return;
      await api("POST", "/api/groups", { name });
    } else if (btn.dataset.groupAction === "delete") {
      const name = btn.dataset.group;
      const count = tags.filter(t => t.parent_id_tag === name).length;
      const effect = count ? ` Its ${count === 1 ? "tag" : `${count} tags`} will no longer be in a group.` : "";
      if (!confirm(`Delete group ${name}?${effect}`)) return;
      await api("DELETE", `/api/groups/${encodeURIComponent(name)}`);
    }
  } catch (err) {
    alert("Failed: " + err.message);
  }
  refresh();
});

// --- Energy chart ---------------------------------------------------------------

const today = new Date();
// Days view: year + month. Months and Quarters views: shownYear.
const energyView = { period: "day", charger: "", year: today.getFullYear(), month: today.getMonth() + 1, shownYear: today.getFullYear() };
let energyChargerPicked = false;  // the user chose in the dropdown; leave it alone
const energyUnchecked = new Set();  // table rows left out of the CSV export
let energyData = [];   // [{period, energy_wh}] for energyView.period and charger
const pad2 = n => String(n).padStart(2, "0");

// Prices, from the settings page.
let currency = "€";
let hasTariff = false;   // whether costs can be shown

async function loadSettings() {
  await localisationReady;
  try {
    const settings = await api("GET", "/api/settings");
    currency = settings.currency;
    hasTariff = settings.tariffs.length > 0;
  } catch { return; }
  if ($("energy")) renderEnergy();
  if ($("sessions")) refresh();
}

// The days the current energy view covers, for the CSV download.
function energyDays() {
  const { period, year, month, shownYear } = energyView;
  const iso = d => d.toLocaleDateString("sv");
  if (period === "day") return [iso(new Date(year, month - 1, 1)), iso(new Date(year, month, 0))];
  if (period === "month" || period === "quarter") return [`${shownYear}-01-01`, `${shownYear}-12-31`];
  return [null, null];
}

function updateEnergyChargers(chargers) {
  const sel = document.getElementById("energy-charger");
  if (!energyChargerPicked && chargers.length === 1) energyView.charger = chargers[0].id;
  setHtml(sel, `<option value="">All chargers</option>` +
    chargers.map(c => `<option value="${esc(c.id)}">${esc(chargerName(c))}</option>`).join(""));
  sel.value = energyView.charger;
}

async function loadEnergy() {
  const { period, charger } = energyView;
  const query = `period=${period}` + (charger ? `&charger_id=${encodeURIComponent(charger)}` : "");
  try {
    const res = await fetch(`/api/energy?${query}`);
    if (!res.ok) return;
    const data = await res.json();
    if (period !== energyView.period || charger !== energyView.charger) return;  // view changed meanwhile
    energyData = data;
    renderEnergy();
  } catch { /* the refresh status already shows the server is unreachable */ }
}

// The columns to show for the current view: [{key, label, title}]
function energyBuckets() {
  const { period, year, month } = energyView;
  if (period === "day") {
    const days = new Date(year, month, 0).getDate();
    return Array.from({ length: days }, (_, i) => {
      const d = new Date(year, month - 1, i + 1);
      return { key: `${year}-${pad2(month)}-${pad2(i + 1)}`, label: String(i + 1),
        title: d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" }) };
    });
  }
  if (period === "month") {
    return Array.from({ length: 12 }, (_, i) => {
      const d = new Date(energyView.shownYear, i, 1);
      return { key: `${d.getFullYear()}-${pad2(d.getMonth() + 1)}`, label: d.toLocaleDateString(undefined, { month: "short" }),
        title: d.toLocaleDateString(undefined, { month: "long", year: "numeric" }) };
    });
  }
  if (period === "quarter") {
    const y = energyView.shownYear, show = m => new Date(y, m, 1).toLocaleDateString(undefined, { month: "short" });
    return Array.from({ length: 4 }, (_, i) => ({ key: `${y}-Q${i + 1}`, label: `Q${i + 1}`,
      title: `Q${i + 1} (${show(3 * i)} – ${show(3 * i + 2)} ${y})` }));
  }
  const current = today.getFullYear();
  const first = Math.min(current, ...energyData.map(e => Number(e.period)));
  return Array.from({ length: current - first + 1 }, (_, i) =>
    ({ key: String(first + i), label: String(first + i), title: String(first + i) }));
}

function energyRangeLabel() {
  const { period, year, month } = energyView;
  if (period === "day") return new Date(year, month - 1, 1).toLocaleDateString(undefined, { month: "long", year: "numeric" });
  return period === "year" ? "All years" : String(energyView.shownYear);
}

// Round axis steps: 1, 2, 2.5 or 5 times a power of ten.
function niceStep(max, ticks = 4) {
  const raw = max / ticks, mag = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw);
}

function barPath(x, y, w, h, r = 4) {
  r = Math.min(r, h, w / 2);  // rounded data end, square at the baseline
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

function renderEnergy() {
  const buckets = energyBuckets();
  const byKey = Object.fromEntries(energyData.map(e => [e.period, e.energy_wh]));
  const values = buckets.map(b => (byKey[b.key] ?? 0) / 1000);  // kWh
  const total = values.reduce((a, b) => a + b, 0);
  const hasData = buckets.some(b => b.key in byKey);

  document.getElementById("energy-range").textContent = energyRangeLabel();
  document.querySelectorAll("[data-energy-nav]").forEach(b => {
    const { period, year, month } = energyView;
    const atEnd = period === "day" ? year === today.getFullYear() && month === today.getMonth() + 1
      : energyView.shownYear === today.getFullYear();
    b.disabled = period === "year" || (b.dataset.energyNav === "1" && atEnd);
  });
  document.querySelectorAll("[data-period]").forEach(b =>
    b.setAttribute("aria-pressed", String(b.dataset.period === energyView.period)));
  const costByKey = Object.fromEntries(energyData.map(e => [e.period, e.cost]));
  const costs = buckets.map(b => costByKey[b.key] ?? null);
  const totalCost = costs.reduce((a, c) => a + (c ?? 0), 0);
  const costText = hasTariff ? ` · <b>${esc(money(totalCost, currency))}</b>` : "";
  document.getElementById("energy-total").innerHTML = hasData
    ? `<b>${esc(kwh(total * 1000))}</b>${costText} used ${energyView.period === "year" ? "in total" : `in ${esc(energyRangeLabel())}`}`
    : `No energy readings for ${esc(energyRangeLabel())}`;
  const el = document.getElementById("energy-svg");
  const W = Math.max(300, el.clientWidth || 600), H = 240;
  const m = { left: 44, right: 6, top: 28, bottom: 24 };
  const pw = W - m.left - m.right, ph = H - m.top - m.bottom;
  const step = niceStep(Math.max(...values, 0) || 1);
  const top = Math.max(step, Math.ceil(Math.max(...values) / step) * step);
  const y = v => m.top + ph - (v / top) * ph;
  const slot = pw / buckets.length, bw = Math.min(24, slot * 0.7);
  const labelEvery = Math.ceil(buckets.length / Math.max(1, Math.floor(pw / 30)));
  const fmt = v => formatNumber(v, { maximumFractionDigits: step < 1 ? 2 : 0 });

  let svg = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Energy used per ${energyView.period}, ${esc(energyRangeLabel())}">`;
  svg += `<text x="${m.left - 8}" y="11" text-anchor="end">kWh</text>`;
  for (let v = 0; v <= top + 1e-9; v += step) {
    svg += `<line class="grid" x1="${m.left}" x2="${W - m.right}" y1="${y(v)}" y2="${y(v)}"/>`;
    svg += `<text x="${m.left - 8}" y="${y(v) + 4}" text-anchor="end">${fmt(v)}</text>`;
  }
  buckets.forEach((b, i) => {
    const cx = m.left + slot * (i + 0.5), v = values[i], h = (v / top) * ph;
    svg += `<rect class="hit" data-i="${i}" x="${m.left + slot * i}" y="${m.top}" width="${slot}" height="${ph}" tabindex="0"
      aria-label="${esc(b.title)}: ${esc(kwh(v * 1000))}"/>`;
    svg += h > 0 ? `<path class="bar" d="${barPath(cx - bw / 2, y(v), bw, h)}"/>` : `<g></g>`;
    if (i % labelEvery === 0) svg += `<text x="${cx}" y="${H - 6}" text-anchor="middle">${esc(b.label)}</text>`;
  });
  setHtml(el, svg + `</svg>`);  // unchanged data keeps hover and focus
  el.dataset.values = JSON.stringify(values);
  el.dataset.costs = JSON.stringify(costs);

  // The price a cost works out to (an average if the price changed), and the cost.
  const costCells = (wh, c, tag = "td") => {
    if (!hasTariff) return "";
    const cents = wh > 0 && c != null ? c / wh * 1e5 : null;
    const price = cents == null ? "—" : `${formatNumber(cents, { maximumFractionDigits: 3 })} c${currency}/kWh`;
    return `<${tag} class="num">${esc(price)}</${tag}><${tag} class="num">${esc(money(c, currency))}</${tag}>`;
  };
  setHtml(document.getElementById("energy-table"), `<table>
    <tr><th class="pick"><input type="checkbox" id="energy-select-all" aria-label="Select all for export" title="Select all"></th><th>${{ day: "Day", month: "Month", quarter: "Quarter", year: "Year" }[energyView.period]}</th><th class="num">Energy</th>${hasTariff ? `<th class="num">Price</th><th class="num">Cost</th>` : ""}</tr>
    ${buckets.map((b, i) => `<tr><td class="pick">${b.key in byKey ? `<input type="checkbox" data-export="${esc(b.key)}" aria-label="Export ${esc(b.title)}">` : ""}</td><td>${esc(b.title)}</td><td class="num">${b.key in byKey ? esc(kwh(values[i] * 1000)) : "—"}</td>${costCells(values[i] * 1000, b.key in byKey ? costs[i] : null)}</tr>`).join("")}
    <tr><th class="pick"></th><th>Total</th><th class="num">${esc(kwh(total * 1000))}</th>${costCells(total * 1000, totalCost, "th")}</tr>
  </table>`);
  updateEnergyExport();
}

// Applies the selection to the checkboxes (which a redraw resets) and the CSV link.
function updateEnergyExport() {
  const boxes = [...document.querySelectorAll("#energy-table input[data-export]")];
  boxes.forEach(b => { b.checked = !energyUnchecked.has(b.dataset.export); });
  const picked = boxes.filter(b => b.checked);
  const all = $("energy-select-all");
  all.disabled = !boxes.length;
  all.checked = boxes.length > 0 && picked.length === boxes.length;
  all.indeterminate = picked.length > 0 && picked.length < boxes.length;

  const links = { csv: $("energy-csv"), pdf: $("energy-pdf") };
  if (!picked.length) {
    for (const link of Object.values(links)) {
      link.removeAttribute("href");
      link.title = "Select at least one row to export";
    }
    return;
  }
  const [firstDay, lastDay] = energyDays();
  const query = new URLSearchParams({ period: energyView.period, decimal: csvDecimal() });
  if (energyView.charger) query.set("charger_id", energyView.charger);
  if (firstDay) { query.set("first_day", firstDay); query.set("last_day", lastDay); }
  if (picked.length < boxes.length) picked.forEach(b => query.append("selected", b.dataset.export));
  for (const [type, link] of Object.entries(links)) {
    link.title = "";
    link.href = `/api/energy.${type}?${query}`;
  }
}

function showEnergyTip(e) {
  const hit = e.target.closest?.(".hit");
  const tip = document.getElementById("energy-tip");
  if (!hit) { tip.style.display = "none"; return; }
  const i = Number(hit.dataset.i), b = energyBuckets()[i];
  const v = JSON.parse(document.getElementById("energy-svg").dataset.values)[i];
  const svg = hit.ownerSVGElement, scale = svg.clientWidth / svg.viewBox.baseVal.width;
  const bar = hit.nextElementSibling, box = bar.getBBox?.();
  const top = box && box.height ? box.y : Number(hit.getAttribute("y")) + Number(hit.getAttribute("height"));
  const cost = JSON.parse(document.getElementById("energy-svg").dataset.costs)[i];
  tip.innerHTML = `${esc(b.title)} · <b>${esc(kwh(v * 1000))}</b>` + (hasTariff && cost != null ? ` · ${esc(money(cost, currency))}` : "");
  tip.style.left = `${(Number(hit.getAttribute("x")) + Number(hit.getAttribute("width")) / 2) * scale}px`;
  tip.style.top = `${top * scale}px`;
  tip.style.display = "block";
}

function setUpEnergy() {
const energyChart = $("energy-chart");
["mouseover", "focusin"].forEach(t => energyChart.addEventListener(t, showEnergyTip));
["mouseleave", "focusout"].forEach(t => energyChart.addEventListener(t, () =>
  document.getElementById("energy-tip").style.display = "none"));

$("energy-table").addEventListener("change", e => {
  const box = e.target;
  if (box.id === "energy-select-all") {
    document.querySelectorAll("#energy-table input[data-export]").forEach(b => {
      if (box.checked) energyUnchecked.delete(b.dataset.export);
      else energyUnchecked.add(b.dataset.export);
    });
  } else if (box.dataset.export !== undefined) {
    if (box.checked) energyUnchecked.delete(box.dataset.export);
    else energyUnchecked.add(box.dataset.export);
  }
  updateEnergyExport();
});

document.getElementById("energy-charger").addEventListener("change", e => {
  energyChargerPicked = true;
  energyView.charger = e.target.value;
  loadEnergy();
});

document.querySelectorAll("[data-period]").forEach(b => b.addEventListener("click", () => {
  energyView.period = b.dataset.period;
  energyData = [];
  renderEnergy();
  loadEnergy();
}));

document.querySelectorAll("[data-energy-nav]").forEach(b => b.addEventListener("click", () => {
  const delta = Number(b.dataset.energyNav);
  if (energyView.period === "day") {
    const d = new Date(energyView.year, energyView.month - 1 + delta, 1);
    energyView.year = d.getFullYear();
    energyView.month = d.getMonth() + 1;
  } else {
    energyView.shownYear += delta;
  }
  renderEnergy();
}));

window.addEventListener("resize", renderEnergy);
loadSettings();
}

if ($("energy")) setUpEnergy();
if ($("session-period")) setUpSessions();

refresh();
setInterval(refresh, 3000);

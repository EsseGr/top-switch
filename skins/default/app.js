"use strict";
// Default skin. Talks only to GET /api/buttons, POST /api/press/<id> and POST /api/groups/<id>.
// The state shown is always what the server read live after the last change (never guessed here).
const KEY = "top-switch-token";
const REFRESH_MS = 30000;
const $ = (id) => document.getElementById(id);

// Token from "#token=..." link: store it and clear the fragment immediately.
if (location.hash.startsWith("#token=")) {
  try {
    const t = decodeURIComponent(location.hash.slice(7));
    if (t) localStorage.setItem(KEY, t);
  } catch (e) {
    // malformed percent-encoding: ignore the token; the login field stays available
  }
  history.replaceState(null, "", location.pathname + location.search);
}

const STATE_TEXT = { on: "Ενεργό", off: "Ανενεργό", partial: "Μερικώς", unknown: "Άγνωστο" };
const INTEGRATION = { adguard: "AdGuard Home", http: "HTTP", webhook: "Webhook", script: "Script" };
let busy = false;

function message(text, kind) {
  const m = $("message");
  m.textContent = text || "";
  m.className = "message" + (kind ? " " + kind : "");
}

function showLogin(text) {
  localStorage.removeItem(KEY);
  $("login").hidden = false;
  $("board").replaceChildren();
  message(text || "");
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { ...(options.headers || {}), Authorization: "Bearer " + localStorage.getItem(KEY) },
  });
  if (res.status === 401) {
    showLogin("Λάθος ή άκυρο token");
    throw new Error("unauthorized");
  }
  return res;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

function ask(question) {
  const dlg = $("ask");
  if (!dlg || typeof dlg.showModal !== "function") return Promise.resolve(window.confirm(question));
  $("ask-text").textContent = question;
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "yes"), { once: true });
    dlg.returnValue = "";
    dlg.showModal();
    $("ask-yes").focus();
  });
}

function stateText(st, labels) {
  const text = (labels && labels[st.state]) || STATE_TEXT[st.state] || st.state;
  if (st.state === "partial" && st.total) return `${text} (${st.blocked ?? st.on}/${st.total})`;
  return text;
}

async function send(path, body, controls) {
  busy = true;
  controls.forEach((c) => { c.disabled = true; });
  message("");
  try {
    const res = await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) message(data.error || "Σφάλμα " + res.status);
    else if (data.warnings && data.warnings.length) message(data.warnings.join(" "), "warn");
  } catch (e) {
    if (e.message === "unauthorized") return;
    message("Αποτυχία επικοινωνίας με τον server");
  } finally {
    busy = false;
  }
  await load(true);
}

async function toggleButton(b, desired, controls) {
  const q = b.confirm === true ? `«${b.name}»: ${desired === "on" ? "ενεργοποίηση" : "απενεργοποίηση"};` : b.confirm;
  if (q && desired !== "off" && !(await ask(q))) return;
  await send("/api/press/" + encodeURIComponent(b.id), desired ? { desired } : {}, controls);
}

async function toggleGroup(g, desired, controls) {
  const q = g.confirm === true ? `«${g.name}»: ${desired === "on" ? "όλες ενεργές" : "όλες ανενεργές"};` : g.confirm;
  if (q && desired !== "off" && !(await ask(q))) return;
  await send("/api/groups/" + encodeURIComponent(g.id), { desired }, controls);
}

function switchFor(state, label, onChange) {
  const sw = el("button", "switch");
  sw.type = "button";
  sw.setAttribute("role", "switch");
  sw.setAttribute("aria-checked", state === "on" ? "true" : state === "partial" ? "mixed" : "false");
  sw.setAttribute("aria-label", label);
  sw.dataset.state = state;
  // partial and unknown → "on" (for a device group "on" = blocked: the safe direction)
  sw.addEventListener("click", () => onChange(state === "on" ? "off" : "on", [sw]));
  return sw;
}

function notes(box, st) {
  if (st.error) box.append(el("p", "note error", st.error));
  if (st.foreign && st.foreign.length) {
    box.append(el("p", "note", "μπλοκαρισμένο και χειροκίνητα στο AdGuard (δεν το αλλάζει το top-switch): " +
      st.foreign.join(", ")));
  }
  (st.warnings || []).forEach((w) => box.append(el("p", "note warn", w)));
}

function chips(sub, uses) {
  (uses || []).forEach((u) => sub.append(el("span", "chip", INTEGRATION[u] || u)));
}

function buttonCard(b) {
  const st = b.status;
  const box = el("section", "card");
  const head = el("div", "head");
  const title = el("div", "title");
  title.append(el("h2", "", b.name));
  const sub = el("div", "sub");
  if (b.toggle) sub.append(el("span", "state " + st.state, stateText(st, b.labels)));
  chips(sub, b.uses);
  if (b.scheduled) sub.append(el("span", "chip", "⏱ πρόγραμμα"));
  title.append(sub);
  head.append(el("div", "emoji", b.icon), title);
  if (!b.toggle) {
    const btn = el("button", "pill", "Πάτημα");
    btn.type = "button";
    btn.addEventListener("click", () => toggleButton(b, null, [btn]));
    head.append(btn);
  } else if (st.state === "unknown") {
    // live state unknown: offer both directions explicitly instead of guessing
    const on = el("button", "pill", (b.labels && b.labels.on) || "Ενεργοποίηση");
    const off = el("button", "pill secondary", (b.labels && b.labels.off) || "Απενεργοποίηση");
    [on, off].forEach((x) => { x.type = "button"; });
    const actions = el("div", "actions");
    actions.append(on, off);
    on.addEventListener("click", () => toggleButton(b, "on", [on, off]));
    off.addEventListener("click", () => toggleButton(b, "off", [on, off]));
    head.append(actions);
  } else {
    head.append(switchFor(st.state, b.name, (d, c) => toggleButton(b, d, c)));
  }
  box.append(head);
  notes(box, st);
  if (b.last_pressed && b.last_pressed.at) {
    box.append(el("p", "note", "τελευταία αλλαγή " + new Date(b.last_pressed.at).toLocaleString("el-GR")));
  }
  return box;
}

function groupCard(g, byId) {
  const members = g.members.map((id) => byId[id]).filter(Boolean);
  const labels = members[0] && members[0].labels;
  const box = el("section", "card");
  const head = el("div", "head");
  const title = el("div", "title");
  title.append(el("h2", "", g.name));
  const sub = el("div", "sub");
  const st = g.status;
  const summary = st.state === "unknown" ? STATE_TEXT.unknown
    : `${(labels && labels.on) || STATE_TEXT.on}: ${st.on} από ${st.total}`;
  sub.append(el("span", "state " + st.state, summary));
  chips(sub, g.integration ? [g.integration] : []);
  title.append(sub);
  head.append(el("div", "emoji", g.icon), title);
  if (g.all_switch) head.append(switchFor(st.state, g.name + ": όλες", (d, c) => toggleGroup(g, d, c)));
  box.append(head);
  const list = el("ul", "devices");
  members.forEach((b) => {
    const row = el("li", "device");
    const t = el("div", "title");
    t.append(el("h2", "", b.name));
    const s = el("div", "sub");
    if (b.detail) s.append(el("span", "ip", b.detail));
    s.append(el("span", "state " + b.status.state, stateText(b.status, b.labels)));
    t.append(s);
    row.append(el("div", "emoji", b.icon), t, switchFor(b.status.state, b.name, (d, c) => toggleButton(b, d, c)));
    list.append(row);
    if (b.status.error || (b.status.warnings || []).length || (b.status.foreign || []).length) {
      const n = el("li", "device notes");
      notes(n, b.status);
      list.append(n);
    }
  });
  box.append(list);
  return box;
}

async function load(keepMessage) {
  if (!localStorage.getItem(KEY)) return showLogin(keepMessage ? undefined : "");
  const r = $("refresh");
  r.classList.add("spin");
  try {
    const res = await api("/api/buttons");
    const data = await res.json();
    $("login").hidden = true;
    const banner = $("banner");
    banner.hidden = !data.insecure;
    banner.textContent = data.insecure ? "ΠΡΟΣΟΧΗ: ο server τρέχει χωρίς token (μόνο για ανάπτυξη)" : "";
    const byId = Object.fromEntries(data.buttons.map((b) => [b.id, b]));
    const cards = (data.groups || []).map((g) => groupCard(g, byId));
    data.buttons.filter((b) => !b.group).forEach((b) => cards.push(buttonCard(b)));
    if (!cards.length) cards.push(el("p", "empty", "Δεν υπάρχουν διακόπτες στο config."));
    $("board").replaceChildren(...cards);
  } catch (e) {
    if (e.message !== "unauthorized") message("Αποτυχία φόρτωσης");
  } finally {
    r.classList.remove("spin");
  }
}

$("login").addEventListener("submit", (ev) => {
  ev.preventDefault();
  localStorage.setItem(KEY, $("token").value.trim());
  $("token").value = "";
  message("");
  load();
});
$("refresh").addEventListener("click", () => load(true));
document.addEventListener("visibilitychange", () => { if (!document.hidden && !busy) load(true); });
setInterval(() => { if (!document.hidden && !busy) load(true); }, REFRESH_MS);

load();

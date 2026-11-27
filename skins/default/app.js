"use strict";
// Default skin: talks only to GET /api/buttons and POST /api/press/<id>.
const KEY = "top-switch-token";
const $ = (id) => document.getElementById(id);

// Token from "#token=..." link: store it and clear the fragment immediately.
if (location.hash.startsWith("#token=")) {
  const t = decodeURIComponent(location.hash.slice(7));
  if (t) localStorage.setItem(KEY, t);
  history.replaceState(null, "", location.pathname + location.search);
}

function message(text) { $("message").textContent = text || ""; }

function showLogin(text) {
  localStorage.removeItem(KEY);
  $("login").hidden = false;
  $("buttons").replaceChildren();
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

const LABELS = { on: "ΕΝΕΡΓΟ", off: "ΑΝΕΝΕΡΓΟ", partial: "ΜΕΡΙΚΟ", unknown: "ΑΓΝΩΣΤΟ" };

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

async function press(b, desired) {
  const question = b.confirm === true ? `Να πατηθεί το «${b.name}»;` : b.confirm;
  if (question && !window.confirm(question)) return;
  message("");
  try {
    const res = await api("/api/press/" + encodeURIComponent(b.id), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: desired ? JSON.stringify({ desired }) : "{}",
    });
    const data = await res.json();
    if (!res.ok) message(data.error || "Σφάλμα " + res.status);
    else if (data.warnings && data.warnings.length) message(data.warnings.join(" "));
  } catch (e) {
    if (e.message !== "unauthorized") message("Αποτυχία επικοινωνίας");
    else return;
  }
  load(true);
}

function card(b) {
  const st = b.status;
  const box = el("section", "card state-" + st.state);
  box.append(el("div", "icon", b.icon), el("h2", "", b.name));
  if (b.toggle) {
    let label = LABELS[st.state];
    if (st.state === "partial") label += ` (${st.blocked}/${st.total})`;
    box.append(el("div", "badge", label));
  }
  const row = el("div", "row");
  const add = (text, desired) => {
    const btn = el("button", "", text);
    btn.type = "button";
    btn.addEventListener("click", () => press(b, desired));
    row.append(btn);
  };
  if (!b.toggle) add("Πάτημα");
  else if (st.state === "unknown") { add("Άνοιξε", "on"); add("Κλείσε", "off"); }
  else add(st.state === "on" ? "Κλείσε" : "Άνοιξε");
  box.append(row);
  if (st.error) box.append(el("p", "note error", st.error));
  if (st.foreign && st.foreign.length) box.append(el("p", "note", "μπλοκαρισμένο εκτός top-switch: " + st.foreign.join(", ")));
  (st.warnings || []).forEach((w) => box.append(el("p", "note warn", w)));
  if (b.last_pressed && b.last_pressed.at) {
    box.append(el("p", "note", "τελευταία ενέργεια " + new Date(b.last_pressed.at).toLocaleString("el-GR")));
  }
  return box;
}

async function load(keepMessage) {
  if (!localStorage.getItem(KEY)) return showLogin(keepMessage ? undefined : "");
  try {
    const res = await api("/api/buttons");
    const data = await res.json();
    $("login").hidden = true;
    const banner = $("banner");
    banner.hidden = !data.insecure;
    banner.textContent = data.insecure ? "ΠΡΟΣΟΧΗ: ο server τρέχει χωρίς token (μόνο για ανάπτυξη)" : "";
    $("buttons").replaceChildren(...data.buttons.map(card));
  } catch (e) {
    if (e.message !== "unauthorized") message("Αποτυχία φόρτωσης");
  }
}

$("login").addEventListener("submit", (ev) => {
  ev.preventDefault();
  localStorage.setItem(KEY, $("token").value.trim());
  $("token").value = "";
  message("");
  load();
});

load();

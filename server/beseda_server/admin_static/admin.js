"use strict";
/* Админка «Беседы». Без библиотек. Все данные выводятся через textContent — никакого innerHTML. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const TOKEN_KEY = "beseda_admin_token";
let token = sessionStorage.getItem(TOKEN_KEY) || "";
let me = null;
let page = "overview";
let timers = [];
let usersCache = [];
let logAfter = 0;

/* ---------- утилиты ---------- */
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
const nf = new Intl.NumberFormat("ru-RU");
function num(n) { return nf.format(n || 0); }
function size(b) {
  b = b || 0;
  const u = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return (i ? b.toFixed(b < 10 ? 1 : 0) : b) + " " + u[i];
}
function dt(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleDateString("ru-RU") + " " + d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}
function ago(ts) {
  if (!ts) return "никогда";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "только что";
  if (s < 3600) return Math.floor(s / 60) + " мин назад";
  if (s < 86400) return Math.floor(s / 3600) + " ч назад";
  if (s < 86400 * 30) return Math.floor(s / 86400) + " дн назад";
  return dt(ts);
}
function duration(s) {
  const d = Math.floor(s / 86400), hr = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return (d ? d + " д " : "") + (d || hr ? hr + " ч " : "") + m + " мин";
}
const COLORS = ["#f47b67", "#f8a532", "#48dd80", "#3fc7eb", "#7289da", "#e78cf1", "#f2d43b", "#4fdcc3", "#ff7a9c", "#a3a6ff"];
function avatar(name, id) {
  const parts = String(name || "?").trim().split(/[\s_.\-]+/).filter(Boolean);
  const ini = parts.length > 1 ? parts[0][0] + parts[1][0] : (parts[0] || "?").slice(0, 2);
  const el = h("span", { class: "av" }, ini.toUpperCase());
  el.style.background = COLORS[Math.abs(Number(id) || 0) % COLORS.length];
  return el;
}
function userCell(name, login, id) {
  return h("div", { class: "user" }, avatar(name, id), h("div", {}, h("b", { text: name }), h("small", { text: "@" + login })));
}
function pill(text, color = "") { return h("span", { class: "pill " + color, text }); }

let toastTimer;
function toast(text, bad = false) {
  const t = $("#toast");
  t.textContent = text;
  t.className = "toast" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 4000);
}

/* ---------- API ---------- */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: Object.assign({ "Authorization": "Bearer " + token },
      opts.body !== undefined ? { "Content-Type": "application/json" } : {}),
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* пусто */ }
  if (res.status === 401 || res.status === 403 && path === "/api/admin/me") {
    logout(data.error || "Сессия истекла");
    throw new Error(data.error || "Нужна авторизация");
  }
  if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}
async function act(path, body, okText) {
  try {
    const r = await api(path, { method: "POST", body });
    if (okText) toast(okText);
    return r;
  } catch (e) { toast(e.message, true); throw e; }
}

/* ---------- модальное окно ---------- */
function ask({ title, text = "", fields = [], ok = "ОК", danger = false, validate }) {
  return new Promise((resolve) => {
    const modal = $("#modal");
    $("#mTitle").textContent = title;
    $("#mText").textContent = text;
    $("#mErr").textContent = "";
    const box = clear($("#mFields"));
    const inputs = {};
    for (const f of fields) {
      let input;
      if (f.type === "select") {
        input = h("select", {}, f.options.map(o => h("option", { value: o.value, text: o.label })));
      } else if (f.type === "checkbox") {
        input = h("input", { type: "checkbox" });
        input.checked = !!f.value;
        box.append(h("label", { class: "switch" }, input, h("span"), " " + f.label));
        inputs[f.name] = input;
        continue;
      } else {
        input = h("input", { type: f.type || "text", placeholder: f.placeholder || "", maxlength: f.max || 200 });
      }
      if (f.value !== undefined && f.type !== "checkbox") input.value = f.value;
      inputs[f.name] = input;
      box.append(h("label", {}, f.label, input));
    }
    const okBtn = $("#mOk");
    okBtn.textContent = ok;
    okBtn.className = "btn " + (danger ? "danger" : "primary");
    modal.classList.remove("hidden");
    const first = Object.values(inputs)[0];
    (first || okBtn).focus();
    const close = (val) => {
      modal.classList.add("hidden");
      $("#modalForm").onsubmit = null;
      $("#mCancel").onclick = null;
      document.removeEventListener("keydown", esc);
      resolve(val);
    };
    const esc = (e) => { if (e.key === "Escape") close(null); };
    document.addEventListener("keydown", esc);
    $("#mCancel").onclick = () => close(null);
    $("#modalForm").onsubmit = (e) => {
      e.preventDefault();
      const vals = {};
      for (const [k, el] of Object.entries(inputs)) vals[k] = el.type === "checkbox" ? el.checked : el.value;
      const problem = validate && validate(vals);
      if (problem) { $("#mErr").textContent = problem; return; }
      close(vals);
    };
  });
}

/* ---------- меню действий ---------- */
/* Меню рисуется в <body> с position:fixed — иначе его обрезает таблица с overflow:auto. */
let openMenu = null;
function closeMenu() { if (openMenu) { openMenu.remove(); openMenu = null; } }
document.addEventListener("click", closeMenu);
window.addEventListener("scroll", closeMenu, true);
window.addEventListener("resize", closeMenu);
function menuButton(items) {
  const btn = h("button", { class: "btn ghost small", type: "button", text: "Действия ▾" });
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    closeMenu();
    const m = h("div", { class: "menu" });
    for (const it of items) {
      if (!it) continue;
      if (it === "-") { m.append(h("hr")); continue; }
      m.append(h("button", { type: "button", class: it.danger ? "danger" : "", text: it.label,
        onclick: (ev) => { ev.stopPropagation(); closeMenu(); it.run(); } }));
    }
    m.style.position = "fixed";
    m.style.visibility = "hidden";
    document.body.append(m);
    const r = btn.getBoundingClientRect();
    const mw = m.offsetWidth, mh = m.offsetHeight;
    let left = Math.min(Math.max(8, r.right - mw), window.innerWidth - mw - 8);
    let top = r.bottom + 4;
    if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 4);  // вверх, если снизу нет места
    m.style.left = left + "px";
    m.style.top = top + "px";
    m.style.right = "auto";
    m.style.visibility = "";
    openMenu = m;
  });
  return btn;
}

/* ---------- вход / выход ---------- */
function showLogin(msg = "") {
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  $("#lErr").textContent = msg;
  $("#lUser").focus();
}
function logout(msg = "") {
  if (token) fetch("/api/logout", { method: "POST", headers: { "Authorization": "Bearer " + token } }).catch(() => {});
  token = "";
  sessionStorage.removeItem(TOKEN_KEY);
  timers.forEach(clearInterval);
  timers = [];
  showLogin(msg);
}
$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#lErr").textContent = "";
  try {
    const res = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("#lUser").value.trim(), password: $("#lPass").value }) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Ошибка входа");
    if (!data.user.is_admin) {
      fetch("/api/logout", { method: "POST", headers: { "Authorization": "Bearer " + data.token } });
      throw new Error("У этой учётной записи нет прав администратора");
    }
    token = data.token;
    sessionStorage.setItem(TOKEN_KEY, token);
    $("#lPass").value = "";
    start();
  } catch (err) { $("#lErr").textContent = err.message; }
});
$("#logout").addEventListener("click", () => logout());

async function start() {
  if (!token) return showLogin();
  try { me = (await api("/api/admin/me")).user; } catch (e) { return; }
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#meName").textContent = me.display_name;
  go(location.hash.slice(1) || "overview");
}

/* ---------- навигация ---------- */
$$("#nav a").forEach(a => a.addEventListener("click", () => go(a.dataset.page)));
window.addEventListener("hashchange", () => { if (token && me) go(location.hash.slice(1) || "overview"); });
const PAGES = { overview: loadOverview, users: loadUsers, guilds: loadGuilds, voice: loadVoice,
                messages: loadMessagesPage, files: loadFiles, settings: loadSettings, logs: loadLogs };
function go(p) {
  if (!PAGES[p]) p = "overview";
  page = p;
  if (location.hash.slice(1) !== p) history.replaceState(null, "", "#" + p);
  $$("#nav a").forEach(a => a.classList.toggle("active", a.dataset.page === p));
  $$("[data-view]").forEach(s => s.classList.toggle("hidden", s.dataset.view !== p));
  timers.forEach(clearInterval);
  timers = [];
  PAGES[p]().catch(e => toast(e.message, true));
  const refresh = { overview: 5000, voice: 2000, logs: 2000, users: 10000 }[p];
  if (refresh) timers.push(setInterval(() => PAGES[p](true).catch(() => {}), refresh));
  timers.push(setInterval(updateVoiceBadge, 5000));
  updateVoiceBadge();
}
async function updateVoiceBadge() {
  try {
    const s = await api("/api/admin/voice");
    const b = $("#voiceBadge");
    b.textContent = s.sessions.length;
    b.classList.toggle("hidden", !s.sessions.length);
  } catch (e) { /* молча */ }
}

/* ---------- Обзор ---------- */
async function loadOverview() {
  const s = await api("/api/admin/stats");
  $("#ver").textContent = "версия " + s.version;
  $("#uptime").textContent = "работает " + duration(s.uptime);
  const cards = [
    ["В сети", num(s.online), s.connections + " подключений", true],
    ["В голосе", num(s.voice_users), s.voice_channels + " каналов", s.voice_users > 0],
    ["Пользователи", num(s.users)],
    ["Серверы", num(s.guilds), num(s.channels) + " каналов"],
    ["Сообщения", num(s.messages), num(s.messages_24h) + " за сутки"],
    ["Файлы", num(s.files), size(s.disk_files) + " на диске"],
    ["База данных", size(s.disk_db)],
  ];
  const box = clear($("#statCards"));
  for (const [k, v, sub, live] of cards) {
    box.append(h("div", { class: "stat" + (live ? " live" : "") }, h("div", { class: "k", text: k }),
      h("div", { class: "v", text: v }), sub ? h("div", { class: "s", text: sub }) : null));
  }
  drawChart(s.per_day);
  const reg = $("#regInfo");
  clear(reg).append(
    s.registration_open ? pill("открыта", "green") : pill("закрыта", "red"), " ",
    s.registration_code ? "по коду " : "без кода",
    s.registration_code ? h("code", { text: s.registration_code }) : "",
    " · ", h("a", { href: "#settings", text: "изменить" }));
}
function drawChart(perDay) {
  const box = clear($("#chart"));
  const days = [];
  const map = new Map(perDay.map(([d, n]) => [d, n]));
  for (let i = 13; i >= 0; i--) {
    const d = new Date(Date.now() - i * 86400000);
    const key = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
    days.push([d, map.get(key) || 0]);
  }
  const max = Math.max(1, ...days.map(x => x[1]));
  const W = 560, H = 190, padL = 30, padB = 22, bw = (W - padL) / days.length;
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  const el = (t, a) => { const e = document.createElementNS(NS, t); for (const k in a) e.setAttribute(k, a[k]); return e; };
  for (const frac of [0, .5, 1]) {
    const y = (H - padB) - frac * (H - padB - 10);
    svg.append(el("line", { x1: padL, x2: W, y1: y, y2: y, class: "grid" }));
    const t = el("text", { x: padL - 6, y: y + 3, "text-anchor": "end" });
    t.textContent = Math.round(max * frac);
    svg.append(t);
  }
  days.forEach(([d, n], i) => {
    const bh = n / max * (H - padB - 10);
    const r = el("rect", { x: padL + i * bw + 3, y: H - padB - bh, width: bw - 6, height: Math.max(bh, n ? 2 : 0), rx: 3, class: "bar" });
    const title = document.createElementNS(NS, "title");
    title.textContent = d.toLocaleDateString("ru-RU") + ": " + n;
    r.append(title);
    svg.append(r);
    if (i % 2 === 0 || days.length < 8) {
      const t = el("text", { x: padL + i * bw + bw / 2, y: H - 6, "text-anchor": "middle" });
      t.textContent = d.getDate() + "." + String(d.getMonth() + 1).padStart(2, "0");
      svg.append(t);
    }
  });
  box.append(svg);
}
$("#bcSend").addEventListener("click", async () => {
  const text = $("#bcText").value.trim();
  if (!text) return toast("Введите текст объявления", true);
  const r = await act("/api/admin/broadcast", { text });
  toast("Объявление отправлено: " + r.delivered + " чел. в сети");
  $("#bcText").value = "";
});

/* ---------- Пользователи ---------- */
async function loadUsers() {
  usersCache = (await api("/api/admin/users")).users;
  renderUsers();
}
$("#userFilter").addEventListener("input", renderUsers);
function renderUsers() {
  const q = $("#userFilter").value.trim().toLowerCase();
  const tb = clear($("#usersTable tbody"));
  const rows = usersCache.filter(u => !q || u.username.toLowerCase().includes(q) || u.display_name.toLowerCase().includes(q));
  if (!rows.length) tb.append(h("tr", {}, h("td", { colspan: 7, class: "empty", text: "Никого не найдено" })));
  for (const u of rows) {
    const st = [];
    if (u.online) st.push(pill("в сети", "green"));
    if (u.voice_channel) st.push(pill("в голосе", "blue"));
    if (u.is_admin) st.push(pill("админ", "yellow"));
    if (u.banned) st.push(pill("заблокирован", "red"));
    if (!st.length) st.push(h("span", { class: "muted", text: "не в сети" }));
    const self = me && u.id === me.id;
    tb.append(h("tr", {},
      h("td", {}, userCell(u.display_name, u.username, u.id)),
      h("td", {}, st),
      h("td", { class: "num", text: num(u.guilds) }),
      h("td", { class: "num", text: num(u.messages) }),
      h("td", { class: "num", text: u.bytes ? size(u.bytes) : "—" }),
      h("td", { class: "muted", text: u.online ? "сейчас" : ago(u.last_seen), title: dt(u.last_seen) }),
      h("td", { class: "actions" }, menuButton([
        { label: "Переименовать…", run: () => renameUser(u) },
        { label: "Сбросить пароль…", run: () => resetPassword(u) },
        { label: "Завершить все сеансы", run: () => userAct(u, { action: "logout_all" }, "Сеансы завершены") },
        { label: "Найти сообщения", run: () => { $("#msgUser").value = u.id; go("messages"); searchMessages({ user_id: u.id }); } },
        "-",
        self ? null : { label: u.is_admin ? "Снять права администратора" : "Сделать администратором",
          run: () => userAct(u, { action: "set_admin", value: !u.is_admin }, u.is_admin ? "Права сняты" : "Теперь администратор") },
        self ? null : { label: u.banned ? "Разблокировать" : "Заблокировать…", danger: !u.banned, run: () => banUser(u) },
      ])),
    ));
  }
}
async function userAct(u, body, okText) {
  const r = await act("/api/admin/users/" + u.id, body, okText);
  await loadUsers();
  return r;
}
async function renameUser(u) {
  const v = await ask({ title: "Отображаемое имя", fields: [{ name: "n", label: "Имя", value: u.display_name, max: 32 }],
    validate: x => x.n.trim() ? "" : "Имя не может быть пустым" });
  if (v) userAct(u, { action: "rename", display_name: v.n.trim() }, "Имя изменено");
}
async function resetPassword(u) {
  const v = await ask({ title: "Сброс пароля: " + u.username,
    text: "Пользователя выкинет со всех устройств. Оставьте поле пустым — пароль будет сгенерирован.",
    fields: [{ name: "p", label: "Новый пароль", placeholder: "сгенерировать автоматически" }],
    validate: x => x.p && x.p.length < 6 ? "Минимум 6 символов" : "" });
  if (!v) return;
  const r = await userAct(u, { action: "reset_password", password: v.p });
  await ask({ title: "Новый пароль", text: "Передайте пользователю " + u.username + ":\n\n" + r.password + "\n\nБольше он нигде не покажется.", ok: "Понятно" });
}
async function banUser(u) {
  if (u.banned) return userAct(u, { action: "ban", value: false }, "Разблокирован");
  const v = await ask({ title: "Заблокировать " + u.display_name + "?", danger: true, ok: "Заблокировать",
    text: "Пользователь будет сразу отключён и не сможет войти, пока вы его не разблокируете. Сообщения останутся." });
  if (v) userAct(u, { action: "ban", value: true }, "Заблокирован");
}
$("#userNew").addEventListener("click", async () => {
  const v = await ask({ title: "Новый пользователь", ok: "Создать", fields: [
    { name: "u", label: "Логин", max: 32 },
    { name: "p", label: "Пароль", placeholder: "пусто — сгенерировать" },
    { name: "a", label: "Администратор", type: "checkbox" }],
    validate: x => !/^[A-Za-zА-Яа-яЁё0-9_.\-]{2,32}$/.test(x.u.trim()) ? "Логин: 2–32 символа, буквы, цифры, _ . -"
      : (x.p && x.p.length < 6 ? "Пароль — минимум 6 символов" : "") });
  if (!v) return;
  const r = await act("/api/admin/users", { username: v.u.trim(), password: v.p, admin: v.a });
  await loadUsers();
  await ask({ title: "Пользователь создан", text: "Логин: " + v.u.trim() + "\nПароль: " + r.password, ok: "Понятно" });
});

/* ---------- Серверы ---------- */
let selectedGuild = null;
async function loadGuilds() {
  const gs = (await api("/api/admin/guilds")).guilds;
  const tb = clear($("#guildsTable tbody"));
  if (!gs.length) tb.append(h("tr", {}, h("td", { colspan: 7, class: "empty", text: "Серверов пока нет" })));
  for (const g of gs) {
    tb.append(h("tr", { class: "clickable" + (g.id === selectedGuild ? " selected" : ""), onclick: () => openGuild(g.id) },
      h("td", {}, h("b", { text: g.name })),
      h("td", { text: g.owner_name || "?" }),
      h("td", { class: "num", text: num(g.members) }),
      h("td", { class: "num", text: num(g.channels) }),
      h("td", { class: "num", text: num(g.messages) }),
      h("td", {}, h("code", { text: g.invite })),
      h("td", { class: "muted", text: dt(g.created) })));
  }
  if (selectedGuild && gs.some(g => g.id === selectedGuild)) openGuild(selectedGuild);
  else { selectedGuild = null; $("#guildDetail").classList.add("hidden"); }
}
async function openGuild(id) {
  selectedGuild = id;
  $$("#guildsTable tbody tr").forEach(r => r.classList.remove("selected"));
  const d = await api("/api/admin/guilds/" + id);
  const g = d.guild;
  const box = clear($("#guildDetail"));
  box.classList.remove("hidden");
  box.className = "detail";
  const names = Object.fromEntries(d.members.map(m => [m.id, m.display_name]));
  const gAct = async (body, okText) => { await act("/api/admin/guilds/" + g.id, body, okText); await loadGuilds(); };
  box.append(h("div", { class: "detail-head" },
    h("h2", { text: g.name }),
    h("div", { class: "row" },
      h("button", { class: "btn ghost", type: "button", text: "Переименовать", onclick: async () => {
        const v = await ask({ title: "Название сервера", fields: [{ name: "n", label: "Название", value: g.name, max: 64 }] });
        if (v && v.n.trim()) gAct({ action: "rename", name: v.n.trim() }, "Переименован");
      } }),
      h("button", { class: "btn ghost", type: "button", text: "Новый код приглашения", onclick: () => gAct({ action: "regen_invite" }, "Код обновлён") }),
      h("button", { class: "btn danger", type: "button", text: "Удалить сервер", onclick: async () => {
        const v = await ask({ title: "Удалить «" + g.name + "»?", danger: true, ok: "Удалить навсегда",
          text: "Будут удалены все каналы, сообщения и файлы. Отменить нельзя.\nДля подтверждения введите название сервера.",
          fields: [{ name: "n", label: "Название" }], validate: x => x.n.trim() === g.name ? "" : "Название не совпадает" });
        if (v) { selectedGuild = null; gAct({ action: "delete" }, "Сервер удалён"); }
      } }))));

  const chans = h("ul", { class: "list" });
  for (const c of d.channels) {
    chans.append(h("li", {},
      h("div", {}, h("b", { text: (c.kind === "text" ? "# " : "🔊 ") + c.name }), " ",
        h("span", { class: "muted small", text: c.kind === "text" ? num(c.messages) + " сообщ." : (c.voice.length ? "сейчас: " + c.voice.map(u => names[u] || u).join(", ") : "пусто") })),
      menuButton([
        { label: "Переименовать…", run: async () => {
          const v = await ask({ title: "Название канала", fields: [{ name: "n", label: "Название", value: c.name, max: 48 }] });
          if (v && v.n.trim()) { await act("/api/admin/channels/" + c.id, { action: "rename", name: v.n.trim() }, "Готово"); openGuild(g.id); }
        } },
        { label: "Удалить канал…", danger: true, run: async () => {
          const v = await ask({ title: "Удалить канал «" + c.name + "»?", danger: true, ok: "Удалить", text: "Вся история канала будет удалена." });
          if (v) { await act("/api/admin/channels/" + c.id, { action: "delete" }, "Канал удалён"); loadGuilds(); }
        } }])));
  }
  const mem = h("ul", { class: "list" });
  for (const m of d.members) {
    const owner = m.id === g.owner_id;
    mem.append(h("li", {}, userCell(m.display_name, m.username, m.id),
      h("div", { class: "row" }, owner ? pill("владелец", "yellow") : null, m.online ? pill("в сети", "green") : null,
        owner ? null : menuButton([
          { label: "Сделать владельцем", run: async () => {
            const v = await ask({ title: "Передать сервер?", text: m.display_name + " станет владельцем «" + g.name + "».", ok: "Передать" });
            if (v) gAct({ action: "transfer", user_id: m.id }, "Владелец изменён");
          } },
          { label: "Выгнать с сервера", danger: true, run: async () => {
            const v = await ask({ title: "Выгнать " + m.display_name + "?", danger: true, ok: "Выгнать",
              text: "Он сможет вернуться по коду приглашения. Чтобы не смог — смените код." });
            if (v) gAct({ action: "kick", user_id: m.id }, "Выгнан");
          } }]))));
  }
  box.append(h("div", { class: "grid2" },
    h("div", { class: "card" }, h("h3", { text: "Каналы · " + d.channels.length }), chans),
    h("div", { class: "card" }, h("h3", { text: "Участники · " + d.members.length }), mem)));
  $$("#guildsTable tbody tr").forEach(r => { if (r.firstChild && r.firstChild.textContent === g.name) r.classList.add("selected"); });
}

/* ---------- Голос ---------- */
async function loadVoice() {
  const s = (await api("/api/admin/voice")).sessions;
  const tb = clear($("#voiceTable tbody"));
  if (!s.length) tb.append(h("tr", {}, h("td", { colspan: 7, class: "empty", text: "Сейчас никто не разговаривает" })));
  for (const v of s) {
    const state = [];
    if (!v.udp) state.push(pill("нет UDP", "red"));
    if (v.deaf) state.push(pill("звук выкл.", "yellow"));
    else if (v.mute) state.push(pill("микрофон выкл.", "yellow"));
    if (v.udp && !v.mute && !v.deaf) state.push(pill("на связи", "green"));
    tb.append(h("tr", {},
      h("td", {}, h("div", { class: "user" }, avatar(v.user, v.user_id), h("b", { text: v.user }))),
      h("td", { text: v.guild + " / " + v.channel }),
      h("td", {}, state),
      h("td", {}, v.addr ? h("code", { text: v.addr }) : "—"),
      h("td", { class: "num", text: num(v.packets_in) }),
      h("td", { class: "num", text: num(v.packets_out) }),
      h("td", { class: "actions" }, h("button", { class: "btn ghost small", type: "button", text: "Отключить",
        onclick: async () => { await act("/api/admin/voice/kick", { user_id: v.user_id }, "Отключён от голоса"); loadVoice(); } }))));
  }
}

/* ---------- Сообщения ---------- */
async function loadMessagesPage(isRefresh) {
  if (isRefresh) return;
  const [gs, us] = await Promise.all([api("/api/admin/guilds"), api("/api/admin/users")]);
  const gSel = $("#msgGuild"), uSel = $("#msgUser");
  const gv = gSel.value, uv = uSel.value;
  clear(gSel).append(h("option", { value: "", text: "Все серверы" }), gs.guilds.map(g => h("option", { value: g.id, text: g.name })));
  clear(uSel).append(h("option", { value: "", text: "Все авторы" }), us.users.map(u => h("option", { value: u.id, text: u.display_name + " (@" + u.username + ")" })));
  gSel.value = gv; uSel.value = uv;
  if (!$("#msgList").childNodes.length) searchMessages();
}
$("#msgForm").addEventListener("submit", (e) => { e.preventDefault(); searchMessages(); });
async function searchMessages(over = {}) {
  const p = new URLSearchParams({ q: $("#msgQ").value.trim(), guild_id: $("#msgGuild").value,
    user_id: over.user_id || $("#msgUser").value, limit: 200 });
  const list = clear($("#msgList"));
  list.append(h("div", { class: "muted", text: "Поиск…" }));
  const ms = (await api("/api/admin/messages?" + p)).messages;
  clear(list);
  if (!ms.length) return list.append(h("div", { class: "card empty", text: "Ничего не найдено" }));
  list.append(h("div", { class: "muted small", text: "Найдено: " + ms.length + (ms.length >= 200 ? " (показаны последние 200)" : "") }));
  for (const m of ms) {
    const item = h("div", { class: "msg" },
      h("div", { class: "meta" }, h("b", { text: m.author }), "  ·  " + m.guild_name + " / #" + m.channel_name + "  ·  " + dt(m.created) +
        (m.edited ? "  ·  изменено" : "") + (m.files ? "  ·  📎 " + m.files : "")),
      h("div", { class: "body", text: m.content || "(только вложения)" }),
      h("div", { class: "actions" }, h("button", { class: "btn ghost small", type: "button", text: "Удалить", onclick: async () => {
        const v = await ask({ title: "Удалить сообщение?", danger: true, ok: "Удалить", text: (m.content || "").slice(0, 300) });
        if (v && await del("/api/admin/messages/" + m.id, "Сообщение удалено")) item.remove();
      } })));
    list.append(item);
  }
}
/* DELETE для сообщений и файлов */
async function del(path, okText) {
  try { await api(path, { method: "DELETE" }); toast(okText); return true; }
  catch (e) { toast(e.message, true); return false; }
}

/* ---------- Файлы ---------- */
async function loadFiles() {
  const fs = (await api("/api/admin/files")).files;
  const total = fs.reduce((a, f) => a + f.size, 0);
  $("#filesSummary").textContent = fs.length ? "Показано " + fs.length + " самых больших файлов, всего " + size(total) : "";
  const tb = clear($("#filesTable tbody"));
  if (!fs.length) tb.append(h("tr", {}, h("td", { colspan: 6, class: "empty", text: "Файлов нет" })));
  for (const f of fs) {
    tb.append(h("tr", {},
      h("td", {}, h("b", { text: f.name }), h("div", { class: "muted small", text: f.mime })),
      h("td", { class: "num", text: size(f.size) }),
      h("td", { text: f.owner }),
      h("td", { class: f.message_id ? "" : "muted", text: f.message_id ? (f.guild_name || "?") + " / #" + (f.channel_name || "?") : "не отправлен" }),
      h("td", { class: "muted", text: dt(f.created) }),
      h("td", { class: "actions" }, h("button", { class: "btn ghost small", type: "button", text: "Удалить", onclick: async () => {
        const v = await ask({ title: "Удалить файл?", danger: true, ok: "Удалить", text: f.name + " (" + size(f.size) + ")\nСообщение останется, вложение из него пропадёт." });
        if (v && await del("/api/admin/files/" + encodeURIComponent(f.id), "Файл удалён")) loadFiles();
      } }))));
  }
}
$("#filesCleanup").addEventListener("click", async () => {
  const v = await ask({ title: "Очистка файлов", ok: "Очистить",
    text: "Удалить файлы, которые загрузили, но так и не отправили в чат, и файлы на диске без записи в базе?" });
  if (!v) return;
  const r = await act("/api/admin/files/cleanup", {});
  toast("Удалено файлов: " + r.removed);
  loadFiles();
});

/* ---------- Настройки ---------- */
async function loadSettings() {
  const s = await api("/api/admin/settings");
  $("#regOpen").checked = s.registration_open;
  $("#regCode").value = s.registration_code || "";
  $("#regEnv").textContent = s.env_registration_code
    ? "В файле настроек задан код «" + s.env_registration_code + "»; значение здесь его перекрывает."
    : "";
  $("#maxUp").value = s.max_upload_mb;
  $("#maxUpEnv").textContent = "В файле настроек сервера: " + s.env_max_upload_mb + " МБ; значение здесь его перекрывает.";
  const kv = clear($("#srvInfo"));
  for (const [k, v] of [["Версия", s.version], ["UDP-порт голоса", s.udp_port], ["Папка данных", s.data_dir]]) {
    kv.append(h("dt", { text: k }), h("dd", { text: String(v) }));
  }
  $("#ver").textContent = "версия " + s.version;
  loadBackups().catch(() => {});
}
$("#regGen").addEventListener("click", () => {
  const a = new Uint8Array(6);
  crypto.getRandomValues(a);
  $("#regCode").value = [...a].map(x => "abcdefghjkmnpqrstuvwxyz23456789"[x % 31]).join("");
});
$("#regSave").addEventListener("click", () =>
  act("/api/admin/settings", { registration_open: $("#regOpen").checked, registration_code: $("#regCode").value.trim() }, "Сохранено"));
$("#maxUpSave").addEventListener("click", () => {
  const mb = parseInt($("#maxUp").value, 10);
  if (!mb || mb < 1 || mb > 4096) return toast("Размер файла: от 1 до 4096 МБ", true);
  act("/api/admin/settings", { max_upload_mb: mb }, "Лимит файла: " + mb + " МБ");
});

/* --- перезагрузка --- */
$("#srvRestart").addEventListener("click", async () => {
  const v = await ask({ title: "Перезагрузить сервер?", ok: "Перезагрузить",
    text: "Все подключения разорвутся примерно на 10 секунд — клиенты переподключатся сами." });
  if (!v) return;
  try { await act("/api/admin/server/restart", {}); } catch (e) { /* сервер уже падает */ }
  toast("Сервер перезагружается…");
  const t0 = Date.now();
  const timer = setInterval(async () => {
    try {
      const r = await fetch("/api/info");
      if (r.ok && Date.now() - t0 > 5000) {
        clearInterval(timer);
        toast("Сервер снова в сети");
        go(page);
      }
    } catch (e) { /* ещё перезагружается */ }
  }, 2000);
  setTimeout(() => clearInterval(timer), 120000);
});

/* --- обновление --- */
$("#updCheck").addEventListener("click", async () => {
  $("#updBox").classList.remove("hidden");
  $("#updInfo").textContent = "Проверяю GitHub…";
  $("#updNotes").classList.add("hidden");
  $("#updRun").classList.add("hidden");
  let r;
  try { r = await api("/api/admin/server/update"); }
  catch (e) { $("#updInfo").textContent = e.message; return; }
  const w = r.watcher;
  $("#watchInfo").textContent = w
    ? "Авто-обновлятор на сервере: " + ({ done: "последнее обновление успешно", running: "идёт обновление…",
        error: "ошибка — " + (w.detail || "") }[w.state] || w.state) + (w.at ? " (" + ago(w.at) + ")" : "")
    : "Авто-обновлятор на сервере ещё не запускался (нужен update-watch.sh в cron — см. README).";
  if (!r.newer) { $("#updInfo").textContent = "Установлена последняя версия (" + r.current + ")."; return; }
  clear($("#updInfo")).append("Доступна версия ", h("b", { text: r.latest }), " (у вас " + r.current + "). ",
    h("a", { href: r.page, target: "_blank", rel: "noopener", text: "Что нового" }));
  if (r.notes) { $("#updNotes").textContent = r.notes; $("#updNotes").classList.remove("hidden"); }
  $("#updRun").classList.remove("hidden");
});
$("#updRun").addEventListener("click", async () => {
  const v = await ask({ title: "Обновить сервер?", ok: "Обновить",
    text: "Обновлятор скачает новую версию из репозитория, пересоберёт контейнер и перезапустит сервер. " +
          "Это займёт несколько минут, в это время чат будет недоступен." });
  if (!v) return;
  await act("/api/admin/server/update", {});
  $("#updRun").classList.add("hidden");
  $("#updInfo").textContent = "Обновление запрошено — сервер перезагрузится в течение пары минут. " +
    "Следите за журналом или вернитесь сюда позже.";
});

/* --- резервные копии --- */
async function loadBackups() {
  const bs = (await api("/api/admin/server/backups")).backups;
  const ul = clear($("#bkList"));
  if (!bs.length) ul.append(h("li", { class: "muted", text: "Копий пока нет" }));
  for (const b of bs) {
    ul.append(h("li", {},
      h("div", {}, h("b", { text: b.name }), " ",
        h("span", { class: "muted small", text: size(b.size) + " · " + dt(b.mtime) })),
      h("div", { class: "row" },
        h("button", { class: "btn ghost small", type: "button", text: "Скачать", onclick: () => downloadBackup(b.name) }),
        h("button", { class: "btn ghost small", type: "button", text: "Удалить", onclick: async () => {
          const v = await ask({ title: "Удалить копию?", danger: true, ok: "Удалить", text: b.name });
          if (v && await del("/api/admin/server/backups/" + encodeURIComponent(b.name), "Копия удалена")) loadBackups();
        } }))));
  }
}
async function downloadBackup(name) {
  try {
    const res = await fetch("/api/admin/server/backups/" + encodeURIComponent(name),
      { headers: { "Authorization": "Bearer " + token } });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const blob = await res.blob();
    const a = h("a", { href: URL.createObjectURL(blob), download: name });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 30000);
  } catch (e) { toast("Не удалось скачать: " + e.message, true); }
}
$("#bkCreate").addEventListener("click", async () => {
  $("#bkCreate").disabled = true;
  try {
    const r = await act("/api/admin/server/backups", {});
    toast("Копия создана: " + r.name + " (" + size(r.size) + ")");
    loadBackups();
  } catch (e) { /* act уже показал ошибку */ }
  $("#bkCreate").disabled = false;
});

/* ---------- Журнал ---------- */
async function loadLogs(isRefresh) {
  const box = $("#logBox");
  if (!isRefresh) { clear(box); logAfter = 0; }
  const lines = (await api("/api/admin/logs?after=" + logAfter)).lines;
  const adminOnly = $("#logAdminOnly").checked;
  for (const l of lines) {
    logAfter = Math.max(logAfter, l.n);
    if (adminOnly && l.name !== "beseda.admin") continue;
    const t = new Date(l.t * 1000).toLocaleTimeString("ru-RU");
    box.append(h("div", { class: l.name === "beseda.admin" ? "adm" : l.level },
      h("span", { class: "t", text: t + " " }), (l.level !== "INFO" ? l.level + " " : "") + l.name + ": " + l.msg));
  }
  while (box.childNodes.length > 2000) box.removeChild(box.firstChild);
  if ($("#logFollow").checked && lines.length) box.scrollTop = box.scrollHeight;
}
$("#logAdminOnly").addEventListener("change", () => loadLogs(false));

start();

"use strict";
/* Публичная страница регистрации «Беседы». Без библиотек. */

const $ = (s) => document.querySelector(s);

async function init() {
  try {
    const r = await fetch("/api/info");
    const info = await r.json();
    document.title = info.name + " — регистрация";
    if (info.name && info.name !== "Беседа") {
      document.querySelector("h1").textContent = info.name;
    }
    $("#ver").textContent = "версия " + info.version;
    const st = $("#srvState");
    if (!info.registration_open) {
      st.textContent = "регистрация закрыта администратором";
      st.className = "pill bad";
      $("#regCard").classList.add("hidden");
      return;
    }
    st.textContent = "сервер в сети · регистрация открыта";
    st.className = "pill ok";
    if (!info.registration_code_required) {
      $("#codeRow").classList.add("hidden");
    }
  } catch (e) {
    const st = $("#srvState");
    st.textContent = "сервер не отвечает — попробуйте позже";
    st.className = "pill bad";
  }
}

$("#regForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#rErr");
  err.textContent = "";
  const user = $("#rUser").value.trim();
  const pass = $("#rPass").value;
  if (pass !== $("#rPass2").value) {
    err.textContent = "Пароли не совпадают";
    return;
  }
  $("#rBtn").disabled = true;
  try {
    const r = await fetch("/api/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: user, password: pass, code: $("#rCode").value.trim() }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || ("Ошибка " + r.status));
    // аккаунт создан; токен нам не нужен — просто показываем инструкцию
    fetch("/api/logout", { method: "POST", headers: { "Authorization": "Bearer " + data.token } }).catch(() => {});
    $("#doneUser").textContent = data.user.display_name || user;
    $("#srvAddr").textContent = location.origin;
    $("#regCard").classList.add("hidden");
    $("#doneCard").classList.remove("hidden");
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (ex) {
    err.textContent = ex.message;
  }
  $("#rBtn").disabled = false;
});

init();

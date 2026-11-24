"""Веб-администрирование: страница /admin и API /api/admin/*.

Доступ — только пользователям с флагом администратора (первый зарегистрированный
становится им автоматически; остальных назначают в админке или командой
python -m beseda_server.manage make-admin ЛОГИН).
"""
import collections
import json
import logging
import os
import re
import secrets
import signal
import threading
import time
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from .app import USERNAME_RE, VERSION, channel_pub, user_pub

log = logging.getLogger("beseda.admin")
STATIC = Path(__file__).parent / "admin_static"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


# ---------------------------------------------------------------- журнал в памяти
class RingLog(logging.Handler):
    """Последние N строк журнала — для вкладки «Журнал» в админке."""

    def __init__(self, size: int = 1500):
        super().__init__(logging.INFO)
        self.lines: collections.deque = collections.deque(maxlen=size)
        self.seq = 0

    def emit(self, record):
        try:
            self.seq += 1
            self.lines.append({"n": self.seq, "t": record.created, "level": record.levelname,
                               "name": record.name, "msg": record.getMessage()})
        except Exception:  # pragma: no cover
            pass


_ring: RingLog | None = None


def ring_log() -> RingLog:
    global _ring
    if _ring is None:
        _ring = RingLog()
        logging.getLogger().addHandler(_ring)
        if logging.getLogger().level > logging.INFO or logging.getLogger().level == logging.NOTSET:
            logging.getLogger().setLevel(logging.INFO)
        for name in ("uvicorn", "uvicorn.error"):
            logging.getLogger(name).addHandler(_ring)
    return _ring


# ---------------------------------------------------------------- маршруты
def admin_routes(hub, auth_user, json_body, err) -> list:
    db, cfg = hub.db, hub.cfg
    started = time.time()
    ring = ring_log()

    def guard(handler):
        async def wrapped(request: Request):
            u = auth_user(request)
            if not u:
                return err("Нужна авторизация", 401)
            if not u["is_admin"]:
                return err("Только для администраторов", 403)
            request.state.admin = u
            try:
                return await handler(request)
            except (KeyError, ValueError, TypeError) as e:
                return err(f"Некорректный запрос: {e}")
        return wrapped

    def audit(request, text: str, *args):
        log.info("[%s] " + text, request.state.admin["username"], *args)

    def ok(**extra):
        return JSONResponse({"ok": True, **extra})

    # --- страница ---
    def static(name: str, mime: str):
        async def handler(request):
            return FileResponse(STATIC / name, media_type=mime, headers={
                "Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-cache", "Referrer-Policy": "no-referrer"})
        return handler

    async def to_slash(request):
        return RedirectResponse("/admin/", status_code=308)

    # --- обзор ---
    async def stats(request):
        t = db.totals()
        voice = {}
        for s in hub.relay.by_ssrc.values():
            voice.setdefault(s.channel_id, []).append(s.user_id)
        disk = sum(p.stat().st_size for p in cfg.files_dir.iterdir() if p.is_file()) if cfg.files_dir.exists() else 0
        db_size = sum(p.stat().st_size for p in cfg.data_dir.glob("beseda.db*"))
        return JSONResponse({
            **t, "version": VERSION, "uptime": time.time() - started,
            "online": hub.online_count(), "connections": sum(len(v) for v in hub.conns.values()),
            "voice_users": len(hub.relay.by_ssrc), "voice_channels": len(voice),
            "disk_files": disk, "disk_db": db_size, "per_day": db.messages_per_day(14),
            "registration_open": hub.reg_open(), "registration_code": hub.reg_code(),
        })

    # --- пользователи ---
    async def users(request):
        out = []
        for r in db.admin_users():
            d = dict(r)
            d["online"] = hub.online(r["id"])
            d["connections"] = len(hub.conns.get(r["id"], ()))
            st = hub.voice_state(r["id"])
            d["voice_channel"] = st["channel_id"] if st else None
            out.append(d)
        return JSONResponse({"users": out})

    async def user_action(request):
        uid = int(request.path_params["uid"])
        u = db.user(uid)
        if not u:
            return err("Пользователь не найден", 404)
        data = await json_body(request)
        act = data.get("action")
        me = request.state.admin
        if act == "set_admin":
            on = bool(data.get("value"))
            if not on and u["is_admin"] and db.admin_count() <= 1:
                return err("Нельзя снять права с последнего администратора")
            db.set_admin(uid, on)
            audit(request, "%s права администратора: %s", "выдал" if on else "снял", u["username"])
            hub.to_users(db.users_sharing_guild(uid) or [uid],
                         {"type": "user_update", "user": hub.user_with_presence(db.user(uid))})
            return ok()
        if act == "ban":
            on = bool(data.get("value"))
            if uid == me["id"]:
                return err("Нельзя заблокировать самого себя")
            if on and u["is_admin"] and db.admin_count() <= 1:
                return err("Нельзя заблокировать последнего администратора")
            db.set_banned(uid, on)
            if on:
                hub.disconnect_user(uid, "Аккаунт заблокирован администратором")
            audit(request, "%s пользователя %s", "заблокировал" if on else "разблокировал", u["username"])
            return ok()
        if act == "reset_password":
            pw = str(data.get("password") or "") or secrets.token_urlsafe(9)
            if len(pw) < 6:
                return err("Пароль — минимум 6 символов")
            db.set_password(uid, pw)
            db.delete_sessions_of(uid)
            hub.disconnect_user(uid, "Пароль изменён администратором — войдите заново")
            audit(request, "сбросил пароль пользователю %s", u["username"])
            return ok(password=pw)
        if act == "logout_all":
            db.delete_sessions_of(uid)
            hub.disconnect_user(uid, "Сеанс завершён администратором")
            audit(request, "завершил все сеансы %s", u["username"])
            return ok()
        if act == "rename":
            name = str(data.get("display_name") or "").strip()[:32]
            if not name:
                return err("Имя не может быть пустым")
            db.set_display_name(uid, name)
            hub.to_users(db.users_sharing_guild(uid) or [uid],
                         {"type": "user_update", "user": hub.user_with_presence(db.user(uid))})
            audit(request, "переименовал %s в «%s»", u["username"], name)
            return ok()
        return err("Неизвестное действие")

    async def user_create(request):
        data = await json_body(request)
        username = str(data.get("username") or "").strip()
        pw = str(data.get("password") or "") or secrets.token_urlsafe(9)
        if not USERNAME_RE.match(username):
            return err("Логин: 2–32 символа, буквы, цифры, _ . -")
        if len(pw) < 6:
            return err("Пароль — минимум 6 символов")
        if db.user_by_name(username):
            return err("Такой логин уже занят", 409)
        uid = db.create_user(username, pw)
        if data.get("admin"):
            db.set_admin(uid, True)
        audit(request, "создал пользователя %s", username)
        return ok(id=uid, password=pw)

    # --- серверы ---
    async def guilds(request):
        return JSONResponse({"guilds": [dict(r) for r in db.admin_guilds()]})

    async def guild_detail(request):
        g = db.guild(int(request.path_params["gid"]))
        if not g:
            return err("Сервер не найден", 404)
        members = []
        for uid in db.member_ids(g["id"]):
            u = db.user(uid)
            if u:
                d = user_pub(u)
                d["online"] = hub.online(uid)
                members.append(d)
        chans = []
        for c in db.channels(g["id"]):
            d = channel_pub(c)
            d["messages"] = db.q1("SELECT COUNT(*) FROM messages WHERE channel_id=?", (c["id"],))[0]
            d["voice"] = [s.user_id for s in hub.relay.by_ssrc.values() if s.channel_id == c["id"]]
            chans.append(d)
        return JSONResponse({"guild": dict(g), "members": members, "channels": chans})

    async def guild_action(request):
        g = db.guild(int(request.path_params["gid"]))
        if not g:
            return err("Сервер не найден", 404)
        data = await json_body(request)
        act = data.get("action")
        if act == "rename":
            name = str(data.get("name") or "").strip()[:64]
            if not name:
                return err("Укажите название")
            db.rename_guild(g["id"], name)
        elif act == "regen_invite":
            db.regen_invite(g["id"])
        elif act == "transfer":
            uid = int(data.get("user_id") or 0)
            if not db.is_member(g["id"], uid):
                return err("Новый владелец должен быть участником сервера")
            db.set_owner(g["id"], uid)
        elif act == "kick":
            uid = int(data.get("user_id") or 0)
            if uid == g["owner_id"]:
                return err("Нельзя выгнать владельца — сначала передайте сервер")
            if db.is_member(g["id"], uid):
                hub._remove_member(g, uid)
            audit(request, "выгнал id=%s с сервера «%s»", uid, g["name"])
            return ok()
        elif act == "delete":
            hub.delete_guild(g)
            audit(request, "удалил сервер «%s»", g["name"])
            return ok()
        else:
            return err("Неизвестное действие")
        g = db.guild(g["id"])
        hub.to_guild(g["id"], {"type": "guild_update", "guild_id": g["id"], "name": g["name"],
                               "invite": g["invite"], "owner_id": g["owner_id"]})
        audit(request, "%s сервер «%s»", {"rename": "переименовал", "regen_invite": "сменил приглашение",
                                          "transfer": "передал другому владельцу"}[act], g["name"])
        return ok()

    async def channel_action(request):
        ch = db.channel(int(request.path_params["cid"]))
        if not ch:
            return err("Канал не найден", 404)
        data = await json_body(request)
        act = data.get("action")
        if act == "rename":
            name = str(data.get("name") or "").strip()[:48]
            if not name:
                return err("Укажите название")
            if ch["kind"] == "text":
                name = name.lower().replace(" ", "-")
            db.rename_channel(ch["id"], name)
            hub.to_guild(ch["guild_id"], {"type": "channel_update", "channel": channel_pub(db.channel(ch["id"]))})
        elif act == "delete":
            uids = [s.user_id for s in hub.relay.by_ssrc.values() if s.channel_id == ch["id"]]
            hub.kick_from_channel_voice(ch["id"])
            for uid in uids:
                hub.to_guild(ch["guild_id"], {"type": "voice_state", "user_id": uid, "channel_id": None,
                                              "guild_id": ch["guild_id"]})
            db.delete_channel(ch["id"])
            hub.to_guild(ch["guild_id"], {"type": "channel_delete", "channel_id": ch["id"], "guild_id": ch["guild_id"]})
        else:
            return err("Неизвестное действие")
        audit(request, "%s канал «%s»", act, ch["name"])
        return ok()

    # --- голос ---
    async def voice(request):
        out = []
        for s in hub.relay.by_ssrc.values():
            ch = db.channel(s.channel_id)
            g = db.guild(ch["guild_id"]) if ch else None
            u = db.user(s.user_id)
            out.append({"user_id": s.user_id, "user": u["display_name"] if u else "?",
                        "channel_id": s.channel_id, "channel": ch["name"] if ch else "?",
                        "guild": g["name"] if g else "?", "mute": s.self_mute, "deaf": s.self_deaf,
                        "udp": bool(s.addr), "addr": f"{s.addr[0]}:{s.addr[1]}" if s.addr else None,
                        "packets_in": s.rx_counter, "packets_out": s.tx_counter})
        return JSONResponse({"sessions": out})

    async def voice_kick(request):
        data = await json_body(request)
        uid = int(data.get("user_id") or 0)
        c = hub.voice_conn.get(uid)
        if c:
            c.send({"type": "voice_closed", "channel_id": None})
            hub.leave_voice(uid)
            audit(request, "отключил от голоса id=%s", uid)
        return ok()

    # --- сообщения ---
    async def messages(request):
        p = request.query_params
        rows = db.search_messages(p.get("q", "").strip(), int(p.get("guild_id") or 0) or None,
                                  int(p.get("user_id") or 0) or None, min(int(p.get("limit") or 100), 500))
        return JSONResponse({"messages": [dict(r) for r in rows]})

    async def message_delete(request):
        m = db.message(int(request.path_params["mid"]))
        if not m:
            return err("Сообщение не найдено", 404)
        hub.delete_message(m)
        audit(request, "удалил сообщение id=%s", m["id"])
        return ok()

    # --- файлы ---
    async def files(request):
        return JSONResponse({"files": [dict(r) for r in db.admin_files(300)]})

    async def file_delete(request):
        f = db.file(request.path_params["fid"])
        if not f:
            return err("Файл не найден", 404)
        (cfg.files_dir / f["id"]).unlink(missing_ok=True)
        db.drop_file_rows([f["id"]])
        if f["message_id"]:
            m = db.message(f["message_id"])
            if m:
                ch = db.channel(m["channel_id"])
                hub.to_guild(ch["guild_id"], {"type": "message_update", "message": hub.message_payload(m)})
        audit(request, "удалил файл «%s»", f["name"])
        return ok()

    async def files_cleanup(request):
        fids = db.orphan_files(time.time())
        for fid in fids:
            (cfg.files_dir / fid).unlink(missing_ok=True)
        db.drop_file_rows(fids)
        known = {r[0] for r in db.q("SELECT id FROM files")}
        stray = [p for p in cfg.files_dir.iterdir() if p.is_file() and p.name not in known] \
            if cfg.files_dir.exists() else []
        for p in stray:
            p.unlink(missing_ok=True)
        audit(request, "очистка файлов: %d неотправленных, %d потерянных", len(fids), len(stray))
        return ok(removed=len(fids) + len(stray))

    # --- настройки и объявления ---
    async def settings_get(request):
        return JSONResponse({"registration_open": hub.reg_open(), "registration_code": hub.reg_code(),
                             "env_registration_code": cfg.registration_code,
                             "max_upload_mb": hub.max_upload_mb(), "env_max_upload_mb": cfg.max_upload_mb,
                             "udp_port": cfg.udp_port, "data_dir": str(cfg.data_dir), "version": VERSION})

    async def settings_set(request):
        data = await json_body(request)
        if "registration_open" in data:
            db.set_setting("reg_open", "1" if data["registration_open"] else "0")
        if "registration_code" in data:
            code = data["registration_code"]
            db.set_setting("reg_code", None if code is None else str(code).strip()[:64])
        if "max_upload_mb" in data:
            v = data["max_upload_mb"]
            if v in (None, "", 0):                       # сброс к значению из beseda.env
                db.set_setting("max_upload_mb", None)
            else:
                mb = int(v)
                if not 1 <= mb <= 4096:
                    return err("Размер файла: от 1 до 4096 МБ")
                db.set_setting("max_upload_mb", mb)
            audit(request, "изменил лимит файла: %s МБ", hub.max_upload_mb())
        audit(request, "изменил настройки: регистрация=%s, код=%s, файл до %s МБ", hub.reg_open(),
              "задан" if hub.reg_code() else "нет", hub.max_upload_mb())
        return ok()

    # --- обслуживание сервера: перезагрузка, обновление, резервные копии ---
    async def server_restart(request):
        audit(request, "перезагрузил сервер из админки")
        # SIGTERM → uvicorn завершается → Docker (restart: unless-stopped) поднимает контейнер снова
        threading.Timer(0.8, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        return ok(detail="Сервер перезагружается")

    def _ver_tuple(text: str) -> tuple:
        m = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(text or ""))
        return tuple(int(x or 0) for x in m.groups()) if m else (0,)

    def _watcher_status() -> dict | None:
        try:
            return json.loads((cfg.data_dir / "update_status.json").read_text("utf-8"))
        except Exception:
            return None

    async def update_check(request):
        import urllib.request

        def _fetch():
            req = urllib.request.Request(
                f"https://api.github.com/repos/{cfg.update_repo}/releases/latest",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "beseda-server"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        try:
            rel = await run_in_threadpool(_fetch)
        except Exception as exc:
            return err(f"Не удалось проверить GitHub: {exc}")
        tag = str(rel.get("tag_name", "")).lstrip("vV")
        return JSONResponse({"current": VERSION, "latest": tag,
                             "newer": _ver_tuple(tag) > _ver_tuple(VERSION),
                             "notes": str(rel.get("body") or "")[:4000],
                             "page": str(rel.get("html_url") or ""),
                             "repo": cfg.update_repo, "watcher": _watcher_status()})

    async def update_run(request):
        (cfg.data_dir / "update_request").write_text(str(time.time()), "utf-8")
        audit(request, "запросил обновление сервера (репозиторий %s)", cfg.update_repo)
        return ok(detail="Запрос принят: обновлятор на сервере подхватит его в течение пары минут, "
                         "затем сервер перезагрузится")

    BACKUP_RE = re.compile(r"^beseda-\d{8}-\d{6}\.tar\.gz$")

    async def backup_create(request):
        from .manage import backup as make_backup
        path = Path(await run_in_threadpool(make_backup, cfg))
        audit(request, "создал резервную копию %s", path.name)
        return ok(name=path.name, size=path.stat().st_size)

    async def backup_list(request):
        d = cfg.data_dir / "backups"
        items = []
        if d.exists():
            for p in sorted(d.glob("beseda-*.tar.gz"), reverse=True)[:50]:
                st = p.stat()
                items.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
        return JSONResponse({"backups": items})

    def _backup_path(request) -> Path | None:
        name = str(request.path_params["name"])
        if not BACKUP_RE.match(name):
            return None
        return cfg.data_dir / "backups" / name

    async def backup_download(request):
        p = _backup_path(request)
        if p is None or not p.exists():
            return err("Файл не найден", 404)
        return FileResponse(p, media_type="application/gzip", filename=p.name)

    async def backup_delete(request):
        p = _backup_path(request)
        if p is None or not p.exists():
            return err("Файл не найден", 404)
        p.unlink()
        audit(request, "удалил резервную копию %s", p.name)
        return ok()

    async def broadcast(request):
        data = await json_body(request)
        text = str(data.get("text") or "").strip()[:500]
        if not text:
            return err("Пустое объявление")
        hub.broadcast_all({"type": "system_notice", "text": text, "from": request.state.admin["display_name"]})
        audit(request, "объявление: %s", text)
        return ok(delivered=hub.online_count())

    async def logs(request):
        after = int(request.query_params.get("after") or 0)
        return JSONResponse({"lines": [x for x in ring.lines if x["n"] > after]})

    async def whoami(request):
        return JSONResponse({"user": user_pub(request.state.admin)})

    A = "/api/admin"
    return [
        Route("/admin", to_slash),
        Route("/admin/", static("index.html", "text/html; charset=utf-8")),
        Route("/admin/admin.js", static("admin.js", "text/javascript; charset=utf-8")),
        Route("/admin/admin.css", static("admin.css", "text/css; charset=utf-8")),
        Route(A + "/me", guard(whoami)),
        Route(A + "/stats", guard(stats)),
        Route(A + "/users", guard(users)),
        Route(A + "/users", guard(user_create), methods=["POST"]),
        Route(A + "/users/{uid:int}", guard(user_action), methods=["POST"]),
        Route(A + "/guilds", guard(guilds)),
        Route(A + "/guilds/{gid:int}", guard(guild_detail)),
        Route(A + "/guilds/{gid:int}", guard(guild_action), methods=["POST"]),
        Route(A + "/channels/{cid:int}", guard(channel_action), methods=["POST"]),
        Route(A + "/voice", guard(voice)),
        Route(A + "/voice/kick", guard(voice_kick), methods=["POST"]),
        Route(A + "/messages", guard(messages)),
        Route(A + "/messages/{mid:int}", guard(message_delete), methods=["DELETE"]),
        Route(A + "/files", guard(files)),
        Route(A + "/files/cleanup", guard(files_cleanup), methods=["POST"]),
        Route(A + "/files/{fid}", guard(file_delete), methods=["DELETE"]),
        Route(A + "/settings", guard(settings_get)),
        Route(A + "/settings", guard(settings_set), methods=["POST"]),
        Route(A + "/server/restart", guard(server_restart), methods=["POST"]),
        Route(A + "/server/update", guard(update_check)),
        Route(A + "/server/update", guard(update_run), methods=["POST"]),
        Route(A + "/server/backups", guard(backup_list)),
        Route(A + "/server/backups", guard(backup_create), methods=["POST"]),
        Route(A + "/server/backups/{name}", guard(backup_download)),
        Route(A + "/server/backups/{name}", guard(backup_delete), methods=["DELETE"]),
        Route(A + "/broadcast", guard(broadcast), methods=["POST"]),
        Route(A + "/logs", guard(logs)),
    ]

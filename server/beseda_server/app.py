"""HTTP API + WebSocket-хаб «Беседы».

REST  — регистрация/вход, загрузка и скачивание файлов.
WS    — всё остальное: сообщения, каналы, серверы, присутствие, управление голосом.
UDP   — сам голос (см. voice.py).
"""
import asyncio
import base64
import contextlib
import logging
import mimetypes
import os
import re
import secrets
import socket
import time
from collections import defaultdict
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .config import Config
from .db import DB, check_password
from .voice import VoiceRelay

VERSION = "1.1.1"
log = logging.getLogger("beseda")

USERNAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9_.\-]{2,32}$")
MAX_MESSAGE = 4000
HISTORY_LIMIT = 50


# ---------------------------------------------------------------- сериализация
def user_pub(u) -> dict:
    return {"id": u["id"], "username": u["username"], "display_name": u["display_name"],
            "is_admin": bool(u["is_admin"]), "is_mod": bool(u["is_mod"]) if "is_mod" in u.keys() else False}


def channel_pub(c) -> dict:
    return {"id": c["id"], "guild_id": c["guild_id"], "name": c["name"], "kind": c["kind"], "position": c["position"]}


def file_pub(f) -> dict:
    return {"id": f["id"], "name": f["name"], "size": f["size"], "mime": f["mime"]}


class ApiError(Exception):
    pass


# ---------------------------------------------------------------- соединение
class Conn:
    def __init__(self, ws: WebSocket, user_id: int):
        self.ws = ws
        self.user_id = user_id
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.voice_ssrc: int | None = None
        self.closed = False

    def send(self, payload: dict):
        if self.closed:
            return
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            # клиент не успевает читать — отключаем, он переподключится
            self.closed = True
            log.warning("user %s: очередь переполнена, отключаю", self.user_id)

    def close(self, final: dict | None = None):
        """Отправить последнее сообщение и закрыть соединение (бан, выход везде)."""
        if self.closed:
            return
        while not self.queue.empty():
            self.queue.get_nowait()
        if final:
            self.queue.put_nowait(final)
        self.queue.put_nowait(None)

    async def writer(self):
        while not self.closed:
            payload = await self.queue.get()
            if payload is None:
                self.closed = True
                break
            await self.ws.send_json(payload)
        with contextlib.suppress(Exception):
            await self.ws.close()


# ---------------------------------------------------------------- хаб
class Hub:
    def __init__(self, cfg: Config, db: DB, relay: VoiceRelay):
        self.cfg = cfg
        self.db = db
        self.relay = relay
        self.conns: dict[int, set[Conn]] = defaultdict(set)
        self.voice_conn: dict[int, Conn] = {}  # user_id -> соединение, владеющее голосовой сессией

    # --- рассылка ---
    def online(self, uid: int) -> bool:
        return bool(self.conns.get(uid))

    def to_user(self, uid: int, payload: dict, except_conn: Conn | None = None):
        for c in list(self.conns.get(uid, ())):
            if c is not except_conn:
                c.send(payload)

    def to_users(self, uids, payload: dict):
        for uid in uids:
            self.to_user(uid, payload)

    def to_guild(self, gid: int, payload: dict, exclude_uid: int | None = None):
        for uid in self.db.member_ids(gid):
            if uid != exclude_uid:
                self.to_user(uid, payload)

    # --- снимки состояния ---
    def user_with_presence(self, u) -> dict:
        d = user_pub(u)
        d["online"] = self.online(u["id"])
        return d

    def voice_state(self, uid: int) -> dict | None:
        c = self.voice_conn.get(uid)
        if not c or c.voice_ssrc is None:
            return None
        s = self.relay.by_ssrc.get(c.voice_ssrc)
        if not s:
            return None
        ch = self.db.channel(s.channel_id)
        return {"user_id": uid, "channel_id": s.channel_id, "guild_id": ch["guild_id"] if ch else None,
                "mute": s.self_mute, "deaf": s.self_deaf, "streaming": bool(s.streaming)}

    def guild_payload(self, g) -> dict:
        return {
            "id": g["id"], "name": g["name"], "owner_id": g["owner_id"], "invite": g["invite"],
            "channels": [channel_pub(c) for c in self.db.channels(g["id"])],
            "members": self.db.member_ids(g["id"]),
        }

    def ready_payload(self, uid: int) -> dict:
        guilds = self.db.guilds_of(uid)
        user_ids = set(self.db.users_sharing_guild(uid)) | {uid}
        users = {}
        for x in user_ids:
            u = self.db.user(x)
            if u:
                users[x] = self.user_with_presence(u)
        guild_ids = {g["id"] for g in guilds}
        voice = []
        for vuid in list(self.voice_conn):
            st = self.voice_state(vuid)
            if st and st["guild_id"] in guild_ids:
                voice.append(st)
        return {
            "type": "ready",
            "me": users[uid],
            "guilds": [self.guild_payload(g) for g in guilds],
            "users": list(users.values()),
            "voice_states": voice,
            "server_version": VERSION,
        }

    # --- сообщения ---
    def message_payload(self, m, files=None, author=None) -> dict:
        if files is None:
            files = self.db.files_of(m["id"])
        if author is None:
            author = self.db.user(m["user_id"])
        return {
            "id": m["id"], "channel_id": m["channel_id"], "author_id": m["user_id"],
            "author_name": author["display_name"] if author else "?",
            "content": m["content"], "created": m["created"], "edited": m["edited"],
            "attachments": [file_pub(f) for f in files],
        }

    # --- проверки доступа ---
    def need_channel(self, uid: int, cid) -> dict:
        ch = self.db.channel(int(cid or 0))
        if not ch or not self.db.is_member(ch["guild_id"], uid):
            raise ApiError("Канал не найден")
        return ch

    def need_owner(self, uid: int, gid) -> dict:
        g = self.db.guild(int(gid or 0))
        if not g or not self.db.is_member(g["id"], uid):
            raise ApiError("Сервер не найден")
        u = self.db.user(uid)
        if g["owner_id"] != uid and not u["is_admin"]:
            raise ApiError("Это может делать только владелец сервера")
        return g

    # --- голос ---
    def leave_voice(self, uid: int, broadcast: bool = True):
        c = self.voice_conn.pop(uid, None)
        if not c or c.voice_ssrc is None:
            return
        s = self.relay.by_ssrc.get(c.voice_ssrc)
        gid = None
        if s:
            ch = self.db.channel(s.channel_id)
            gid = ch["guild_id"] if ch else None
        self.relay.close_session(c.voice_ssrc)
        c.voice_ssrc = None
        if broadcast and gid:
            self.to_guild(gid, {"type": "voice_state", "user_id": uid, "channel_id": None, "guild_id": gid})

    def broadcast_voice_state(self, uid: int):
        st = self.voice_state(uid)
        if st and st["guild_id"]:
            self.to_guild(st["guild_id"], {"type": "voice_state", **st})

    def kick_from_channel_voice(self, cid: int):
        for s in self.relay.close_channel(cid):
            c = self.voice_conn.pop(s.user_id, None)
            if c:
                c.voice_ssrc = None
                c.send({"type": "voice_closed", "channel_id": cid})

    # ---------------------------------------------------------- обработчики WS
    async def handle(self, conn: Conn, msg: dict):
        op = msg.get("type")
        fn = getattr(self, f"op_{op}", None) if isinstance(op, str) else None
        if fn is None:
            raise ApiError(f"Неизвестная операция: {op}")
        reply = fn(conn, msg)
        if reply is not None:
            if "req" in msg:
                reply["req"] = msg["req"]
            conn.send(reply)

    def op_ping(self, conn, msg):
        return {"type": "pong", "t": msg.get("t")}

    # сообщения
    def op_history(self, conn, msg):
        ch = self.need_channel(conn.user_id, msg.get("channel_id"))
        if ch["kind"] != "text":
            raise ApiError("Это голосовой канал")
        before = msg.get("before")
        limit = max(1, min(int(msg.get("limit") or HISTORY_LIMIT), 100))
        rows = self.db.history(ch["id"], int(before) if before else None, limit)
        files = self.db.files_of_many([r["id"] for r in rows])
        authors = {}
        out = []
        for r in rows:
            if r["user_id"] not in authors:
                authors[r["user_id"]] = self.db.user(r["user_id"])
            out.append(self.message_payload(r, files.get(r["id"], []), authors[r["user_id"]]))
        return {"type": "history", "channel_id": ch["id"], "messages": out,
                "before": before, "has_more": len(rows) == limit}

    def op_send(self, conn, msg):
        ch = self.need_channel(conn.user_id, msg.get("channel_id"))
        if ch["kind"] != "text":
            raise ApiError("Нельзя писать в голосовой канал")
        content = str(msg.get("content") or "").strip()
        fids = [str(f) for f in (msg.get("files") or [])][:10]
        if not content and not fids:
            raise ApiError("Пустое сообщение")
        if len(content) > MAX_MESSAGE:
            raise ApiError(f"Сообщение длиннее {MAX_MESSAGE} символов")
        mid = self.db.add_message(ch["id"], conn.user_id, content)
        files = self.db.attach_files(mid, conn.user_id, fids) if fids else []
        payload = {"type": "message_create", "message": self.message_payload(self.db.message(mid), files),
                   "nonce": msg.get("nonce")}
        self.to_guild(ch["guild_id"], payload)
        return None

    def op_edit(self, conn, msg):
        m = self.db.message(int(msg.get("message_id") or 0))
        if not m or m["user_id"] != conn.user_id:
            raise ApiError("Можно редактировать только свои сообщения")
        content = str(msg.get("content") or "").strip()
        if not content or len(content) > MAX_MESSAGE:
            raise ApiError("Некорректный текст")
        self.db.edit_message(m["id"], content)
        ch = self.db.channel(m["channel_id"])
        self.to_guild(ch["guild_id"], {"type": "message_update", "message": self.message_payload(self.db.message(m["id"]))})

    def op_delete(self, conn, msg):
        m = self.db.message(int(msg.get("message_id") or 0))
        if not m:
            raise ApiError("Сообщение не найдено")
        ch = self.db.channel(m["channel_id"])
        g = self.db.guild(ch["guild_id"])
        u = self.db.user(conn.user_id)
        if m["user_id"] != conn.user_id and g["owner_id"] != conn.user_id and not u["is_admin"]:
            raise ApiError("Нет прав на удаление")
        self.delete_message(m)

    # --- общие действия (используются и клиентами, и админкой) ---
    def delete_message(self, m):
        ch = self.db.channel(m["channel_id"])
        for fid in self.db.delete_message(m["id"]):
            with contextlib.suppress(FileNotFoundError):
                (self.cfg.files_dir / fid).unlink()
        if ch:
            self.to_guild(ch["guild_id"], {"type": "message_delete", "message_id": m["id"], "channel_id": ch["id"]})

    def delete_guild(self, g):
        members = self.db.member_ids(g["id"])
        for c in self.db.channels(g["id"]):
            if c["kind"] == "voice":
                self.kick_from_channel_voice(c["id"])
        self.db.delete_guild(g["id"])
        self.to_users(members, {"type": "guild_remove", "guild_id": g["id"]})

    def disconnect_user(self, uid: int, reason: str):
        """Выкинуть пользователя со всех устройств (бан, сброс пароля, «выйти везде»)."""
        if uid in self.voice_conn:
            self.leave_voice(uid)
        for c in list(self.conns.get(uid, ())):
            c.close({"type": "auth_failed", "reason": reason})

    def broadcast_all(self, payload: dict):
        for uid in list(self.conns):
            self.to_user(uid, payload)

    def online_count(self) -> int:
        return len(self.conns)

    # настройки регистрации: значение из админки важнее, чем из /etc/beseda.env
    def reg_code(self) -> str:
        v = self.db.get_setting("reg_code")
        return self.cfg.registration_code if v is None else v

    def reg_open(self) -> bool:
        return self.db.get_setting("reg_open", "1") != "0"

    def max_upload_mb(self) -> int:
        """Лимит размера файла: значение из админки важнее, чем из beseda.env."""
        v = self.db.get_setting("max_upload_mb")
        try:
            return max(1, int(v)) if v is not None else self.cfg.max_upload_mb
        except (TypeError, ValueError):
            return self.cfg.max_upload_mb

    def op_typing(self, conn, msg):
        ch = self.need_channel(conn.user_id, msg.get("channel_id"))
        self.to_guild(ch["guild_id"], {"type": "typing", "channel_id": ch["id"], "user_id": conn.user_id},
                      exclude_uid=conn.user_id)

    # голос
    def op_voice_join(self, conn, msg):
        ch = self.need_channel(conn.user_id, msg.get("channel_id"))
        if ch["kind"] != "voice":
            raise ApiError("Это не голосовой канал")
        old = self.voice_conn.get(conn.user_id)
        if old is not None and old is not conn:
            old.send({"type": "voice_closed", "channel_id": None})  # зашёл в голос с другого устройства
        self.leave_voice(conn.user_id)
        s = self.relay.open_session(conn.user_id, ch["id"])
        s.self_mute = bool(msg.get("mute"))
        s.self_deaf = bool(msg.get("deaf"))
        conn.voice_ssrc = s.ssrc
        self.voice_conn[conn.user_id] = conn
        conn.send({
            "type": "voice_ready", "channel_id": ch["id"], "ssrc": s.ssrc,
            "key": base64.b64encode(s.key).decode(),
            "udp_host": self.cfg.udp_public_host, "udp_port": self.cfg.udp_port,
            "req": msg.get("req"),
        })
        self.broadcast_voice_state(conn.user_id)

    def op_voice_leave(self, conn, msg):
        if self.voice_conn.get(conn.user_id) is conn:
            self.leave_voice(conn.user_id)

    def op_voice_update(self, conn, msg):
        if self.voice_conn.get(conn.user_id) is not conn or conn.voice_ssrc is None:
            return None
        s = self.relay.by_ssrc.get(conn.voice_ssrc)
        if s:
            s.self_mute = bool(msg.get("mute"))
            s.self_deaf = bool(msg.get("deaf"))
            self.broadcast_voice_state(conn.user_id)

    def op_voice_stream(self, conn, msg):
        """Начал/закончил демонстрацию экрана — сообщаем всему серверу."""
        if self.voice_conn.get(conn.user_id) is not conn or conn.voice_ssrc is None:
            return None
        s = self.relay.by_ssrc.get(conn.voice_ssrc)
        if not s:
            return None
        s.streaming = bool(msg.get("streaming"))
        if not s.streaming:
            for o in self.relay.by_ssrc.values():
                o.watch.discard(s.ssrc)
        self.broadcast_voice_state(conn.user_id)

    def op_video_watch(self, conn, msg):
        """Подписка на чужую трансляцию (или отписка)."""
        if self.voice_conn.get(conn.user_id) is not conn or conn.voice_ssrc is None:
            raise ApiError("Сначала подключитесь к голосовому каналу")
        me = self.relay.by_ssrc.get(conn.voice_ssrc)
        target_uid = int(msg.get("user_id") or 0)
        tconn = self.voice_conn.get(target_uid)
        ts = self.relay.by_ssrc.get(tconn.voice_ssrc) if tconn and tconn.voice_ssrc else None
        if not me or not ts or ts.channel_id != me.channel_id:
            raise ApiError("Этот пользователь не в вашем голосовом канале")
        if msg.get("watch"):
            me.watch.add(ts.ssrc)
        else:
            me.watch.discard(ts.ssrc)

    # серверы
    def op_guild_create(self, conn, msg):
        name = str(msg.get("name") or "").strip()[:64]
        if not name:
            raise ApiError("Укажите название")
        gid = self.db.create_guild(name, conn.user_id)
        self.to_user(conn.user_id, {"type": "guild_add", "guild": self.guild_payload(self.db.guild(gid)),
                                    "users": [], "voice_states": []})

    def op_guild_join(self, conn, msg):
        invite = str(msg.get("invite") or "").strip().rsplit("/", 1)[-1]
        g = self.db.guild_by_invite(invite)
        if not g:
            raise ApiError("Приглашение недействительно")
        if self.db.is_member(g["id"], conn.user_id):
            raise ApiError("Вы уже на этом сервере")
        self.db.add_member(g["id"], conn.user_id)
        me = self.user_with_presence(self.db.user(conn.user_id))
        self.to_guild(g["id"], {"type": "member_join", "guild_id": g["id"], "user": me}, exclude_uid=conn.user_id)
        users = [self.user_with_presence(self.db.user(x)) for x in self.db.member_ids(g["id"])]
        voice = [st for st in (self.voice_state(x) for x in list(self.voice_conn)) if st and st["guild_id"] == g["id"]]
        self.to_user(conn.user_id, {"type": "guild_add", "guild": self.guild_payload(g), "users": users,
                                    "voice_states": voice})

    def op_guild_leave(self, conn, msg):
        g = self.db.guild(int(msg.get("guild_id") or 0))
        if not g or not self.db.is_member(g["id"], conn.user_id):
            raise ApiError("Сервер не найден")
        if g["owner_id"] == conn.user_id:
            raise ApiError("Владелец не может выйти — удалите сервер или передайте его")
        self._remove_member(g, conn.user_id)

    def op_guild_kick(self, conn, msg):
        g = self.need_owner(conn.user_id, msg.get("guild_id"))
        uid = int(msg.get("user_id") or 0)
        if uid == g["owner_id"]:
            raise ApiError("Нельзя выгнать владельца")
        if self.db.is_member(g["id"], uid):
            self._remove_member(g, uid)

    def _remove_member(self, g, uid: int):
        st = self.voice_state(uid)
        if st and st["guild_id"] == g["id"]:
            self.leave_voice(uid)
        self.db.remove_member(g["id"], uid)
        self.to_user(uid, {"type": "guild_remove", "guild_id": g["id"]})
        self.to_guild(g["id"], {"type": "member_leave", "guild_id": g["id"], "user_id": uid})

    def op_guild_update(self, conn, msg):
        g = self.need_owner(conn.user_id, msg.get("guild_id"))
        name = str(msg.get("name") or "").strip()[:64]
        if name:
            self.db.rename_guild(g["id"], name)
        if msg.get("regen_invite"):
            self.db.regen_invite(g["id"])
        g = self.db.guild(g["id"])
        self.to_guild(g["id"], {"type": "guild_update", "guild_id": g["id"], "name": g["name"],
                                "invite": g["invite"], "owner_id": g["owner_id"]})

    def op_guild_delete(self, conn, msg):
        self.delete_guild(self.need_owner(conn.user_id, msg.get("guild_id")))

    # каналы
    def op_channel_create(self, conn, msg):
        g = self.need_owner(conn.user_id, msg.get("guild_id"))
        kind = msg.get("kind")
        if kind not in ("text", "voice"):
            raise ApiError("Тип канала: text или voice")
        name = str(msg.get("name") or "").strip()[:48]
        if not name:
            raise ApiError("Укажите название")
        if kind == "text":
            name = name.lower().replace(" ", "-")
        cid = self.db.create_channel(g["id"], name, kind)
        self.to_guild(g["id"], {"type": "channel_create", "channel": channel_pub(self.db.channel(cid))})

    def op_channel_update(self, conn, msg):
        ch = self.need_channel(conn.user_id, msg.get("channel_id"))
        self.need_owner(conn.user_id, ch["guild_id"])
        name = str(msg.get("name") or "").strip()[:48]
        if not name:
            raise ApiError("Укажите название")
        if ch["kind"] == "text":
            name = name.lower().replace(" ", "-")
        self.db.rename_channel(ch["id"], name)
        self.to_guild(ch["guild_id"], {"type": "channel_update", "channel": channel_pub(self.db.channel(ch["id"]))})

    def op_channel_delete(self, conn, msg):
        ch = self.need_channel(conn.user_id, msg.get("channel_id"))
        self.need_owner(conn.user_id, ch["guild_id"])
        if ch["kind"] == "voice":
            uids = [s.user_id for s in self.relay.by_ssrc.values() if s.channel_id == ch["id"]]
            self.kick_from_channel_voice(ch["id"])
            for uid in uids:
                self.to_guild(ch["guild_id"], {"type": "voice_state", "user_id": uid, "channel_id": None,
                                               "guild_id": ch["guild_id"]})
        self.db.delete_channel(ch["id"])
        self.to_guild(ch["guild_id"], {"type": "channel_delete", "channel_id": ch["id"], "guild_id": ch["guild_id"]})

    # профиль
    def op_set_display_name(self, conn, msg):
        name = str(msg.get("display_name") or "").strip()[:32]
        if not name:
            raise ApiError("Имя не может быть пустым")
        self.db.set_display_name(conn.user_id, name)
        payload = {"type": "user_update", "user": self.user_with_presence(self.db.user(conn.user_id))}
        self.to_users(self.db.users_sharing_guild(conn.user_id) or [conn.user_id], payload)

    # --- подключение/отключение ---
    def on_connect(self, conn: Conn):
        first = not self.online(conn.user_id)
        self.conns[conn.user_id].add(conn)
        self.db.touch(conn.user_id)
        conn.send(self.ready_payload(conn.user_id))
        if first:
            self._presence(conn.user_id, True)

    def on_disconnect(self, conn: Conn):
        conn.closed = True
        self.db.touch(conn.user_id)
        if self.voice_conn.get(conn.user_id) is conn:
            self.leave_voice(conn.user_id)
        self.conns[conn.user_id].discard(conn)
        if not self.conns[conn.user_id]:
            self.conns.pop(conn.user_id, None)
            self._presence(conn.user_id, False)

    def _presence(self, uid: int, online: bool):
        payload = {"type": "presence", "user_id": uid, "online": online}
        for x in self.db.users_sharing_guild(uid):
            if x != uid:
                self.to_user(x, payload)


# ---------------------------------------------------------------- приложение
def create_app(cfg: Config | None = None) -> Starlette:
    from .admin import admin_routes, ring_log  # здесь, чтобы не было циклического импорта
    ring_log()
    cfg = cfg or Config()
    cfg.files_dir.mkdir(parents=True, exist_ok=True)
    db = DB(cfg.db_path)
    relay = VoiceRelay()
    hub = Hub(cfg, db, relay)
    login_fails: dict[str, list[float]] = defaultdict(list)

    def client_ip(request) -> str:
        ip = request.client.host if request.client else "?"
        if ip in ("127.0.0.1", "::1"):
            ip = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", ip).split(",")[0].strip()
        return ip

    def throttled(ip: str) -> bool:
        now = time.time()
        login_fails[ip] = [t for t in login_fails[ip] if now - t < 300]
        return len(login_fails[ip]) >= 10

    def auth_user(request: Request):
        h = request.headers.get("authorization", "")
        token = h[7:] if h.lower().startswith("bearer ") else ""
        return db.user_by_token(token)

    def err(text: str, code: int = 400):
        return JSONResponse({"error": text}, status_code=code)

    async def json_body(request: Request) -> dict:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # --- REST ---
    async def info(request: Request):
        return JSONResponse({"name": "Беседа", "version": VERSION,
                             "registration_open": hub.reg_open(),
                             "registration_code_required": bool(hub.reg_code()),
                             "max_upload_mb": hub.max_upload_mb()})

    async def register(request: Request):
        ip = client_ip(request)
        if throttled(ip):
            return err("Слишком много попыток, подождите 5 минут", 429)
        data = await json_body(request)
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        if not hub.reg_open():
            return err("Регистрация на сервере закрыта администратором", 403)
        code = hub.reg_code()
        if code and not secrets.compare_digest(str(data.get("code") or "").strip().encode(), code.encode()):
            login_fails[ip].append(time.time())
            return err("Неверный код регистрации", 403)
        if not USERNAME_RE.match(username):
            return err("Логин: 2–32 символа, буквы, цифры, _ . -")
        if len(password) < 6:
            return err("Пароль — минимум 6 символов")
        if db.user_by_name(username):
            return err("Такой логин уже занят", 409)
        uid = db.create_user(username, password)
        return JSONResponse({"token": db.create_session(uid), "user": user_pub(db.user(uid))})

    async def login(request: Request):
        ip = client_ip(request)
        if throttled(ip):
            return err("Слишком много попыток, подождите 5 минут", 429)
        data = await json_body(request)
        u = db.user_by_name(str(data.get("username") or "").strip())
        if not u or not check_password(str(data.get("password") or ""), u["pw_hash"]):
            login_fails[ip].append(time.time())
            return err("Неверный логин или пароль", 401)
        if u["banned"]:
            return err("Аккаунт заблокирован администратором", 403)
        return JSONResponse({"token": db.create_session(u["id"]), "user": user_pub(u)})

    async def logout(request: Request):
        h = request.headers.get("authorization", "")
        if h.lower().startswith("bearer "):
            db.delete_session(h[7:])
        return JSONResponse({"ok": True})

    async def change_password(request: Request):
        u = auth_user(request)
        if not u:
            return err("Нужна авторизация", 401)
        data = await json_body(request)
        if not check_password(str(data.get("old") or ""), u["pw_hash"]):
            return err("Старый пароль неверен", 403)
        new = str(data.get("new") or "")
        if len(new) < 6:
            return err("Пароль — минимум 6 символов")
        db.set_password(u["id"], new)
        return JSONResponse({"ok": True})

    async def upload(request: Request):
        u = auth_user(request)
        if not u:
            return err("Нужна авторизация", 401)
        mb = hub.max_upload_mb()
        limit = mb * 1024 * 1024
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > limit + 64 * 1024:
            return err(f"Файл больше {mb} МБ", 413)
        form = await request.form(max_files=1, max_fields=5)
        f = form.get("file")
        if f is None or not hasattr(f, "read"):
            return err("Нет файла")
        name = os.path.basename((f.filename or "file").replace("\\", "/"))[:200] or "file"
        fid = secrets.token_hex(16)
        path = cfg.files_dir / fid
        size = 0
        try:
            with open(path, "wb") as out:
                while chunk := await f.read(1024 * 256):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError
                    out.write(chunk)
        except ValueError:
            path.unlink(missing_ok=True)
            return err(f"Файл больше {mb} МБ", 413)
        finally:
            await form.close()
        mime = f.content_type if f.content_type and f.content_type != "application/octet-stream" else None
        mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
        db.add_file(fid, u["id"], name, size, mime)
        return JSONResponse({"id": fid, "name": name, "size": size, "mime": mime})

    async def download(request: Request):
        u = auth_user(request)
        if not u:
            return err("Нужна авторизация", 401)
        f = db.file(request.path_params["fid"])
        if not f:
            return err("Файл не найден", 404)
        allowed = f["user_id"] == u["id"]
        if not allowed and f["message_id"]:
            m = db.message(f["message_id"])
            ch = db.channel(m["channel_id"]) if m else None
            allowed = bool(ch and db.is_member(ch["guild_id"], u["id"]))
        if not allowed:
            return err("Файл не найден", 404)
        path = cfg.files_dir / f["id"]
        if not path.exists():
            return err("Файл пропал с диска", 410)
        inline = f["mime"].startswith("image/")
        return FileResponse(path, media_type=f["mime"], filename=f["name"],
                            content_disposition_type="inline" if inline else "attachment",
                            headers={"Cache-Control": "private, max-age=86400"})

    # --- WebSocket ---
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        try:
            first = await asyncio.wait_for(ws.receive_json(), timeout=15)
        except Exception:
            await ws.close(code=4000)
            return
        u = db.user_by_token(str(first.get("token") or "")) if isinstance(first, dict) and first.get("type") == "auth" else None
        if not u:
            await ws.send_json({"type": "auth_failed"})
            await ws.close(code=4001)
            return
        conn = Conn(ws, u["id"])
        writer = asyncio.create_task(conn.writer())
        hub.on_connect(conn)
        try:
            while not conn.closed:
                msg = await ws.receive_json()
                if not isinstance(msg, dict):
                    continue
                try:
                    await hub.handle(conn, msg)
                except ApiError as e:
                    conn.send({"type": "error", "message": str(e), "req": msg.get("req"), "op": msg.get("type")})
                except Exception:
                    log.exception("ошибка обработки %s", msg.get("type"))
                    conn.send({"type": "error", "message": "Внутренняя ошибка сервера", "req": msg.get("req")})
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception:
            log.exception("ws error")
        finally:
            hub.on_disconnect(conn)
            writer.cancel()
            with contextlib.suppress(Exception):
                await ws.close()

    # --- фоновые задачи ---
    async def cleanup_loop():
        while True:
            await asyncio.sleep(3600)
            try:
                fids = db.orphan_files(time.time() - 24 * 3600)
                for fid in fids:
                    (cfg.files_dir / fid).unlink(missing_ok=True)
                db.drop_file_rows(fids)
                if fids:
                    log.info("удалено %d неотправленных файлов", len(fids))
            except Exception:
                log.exception("cleanup")

    @contextlib.asynccontextmanager
    async def lifespan(app):
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            # Windows: иначе ICMP «порт недоступен» от ушедшего клиента ломает приём (WinError 10054)
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        sock.bind((cfg.udp_host, cfg.udp_port))
        sock.setblocking(False)
        transport, _ = await loop.create_datagram_endpoint(lambda: relay, sock=sock)
        log.info("UDP голос: %s:%s", cfg.udp_host, cfg.udp_port)
        task = asyncio.create_task(cleanup_loop())
        from .scheduler import scheduler_loop
        sched = asyncio.create_task(scheduler_loop(hub, cfg))
        try:
            yield
        finally:
            task.cancel()
            sched.cancel()
            transport.close()

    WEB_STATIC = Path(__file__).parent / "web_static"
    SITE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")

    def site(name: str, mime: str):
        """Публичная страница регистрации (лендинг)."""
        async def handler(request):
            return FileResponse(WEB_STATIC / name, media_type=mime, headers={
                "Content-Security-Policy": SITE_CSP, "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-cache", "Referrer-Policy": "no-referrer"})
        return handler

    app = Starlette(
        routes=[
            Route("/", site("index.html", "text/html; charset=utf-8")),
            Route("/site.css", site("site.css", "text/css; charset=utf-8")),
            Route("/site.js", site("site.js", "text/javascript; charset=utf-8")),
            Route("/api/info", info),
            Route("/api/register", register, methods=["POST"]),
            Route("/api/login", login, methods=["POST"]),
            Route("/api/logout", logout, methods=["POST"]),
            Route("/api/password", change_password, methods=["POST"]),
            Route("/api/upload", upload, methods=["POST"]),
            Route("/api/files/{fid}", download),
            WebSocketRoute("/ws", ws_endpoint),
            *admin_routes(hub, auth_user, json_body, err, client_ip),
        ],
        lifespan=lifespan,
    )
    app.state.hub = hub
    app.state.db = db
    app.state.relay = relay
    app.state.cfg = cfg
    return app

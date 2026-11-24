"""Главное окно: серверы | каналы | чат | участники."""
import base64
import ctypes
import os
import sys
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QGuiApplication, QIcon
from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea,
                               QSlider, QSystemTrayIcon, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
                               QWidgetAction)

from ..api import Api, run_bg
from ..gateway import Gateway
from ..voice import VoiceEngine
from .chat import ChatPanel, ImageCache, cache_dir, open_local
from .settings import SettingsDialog, apply_audio_settings
from .style import GREEN, MUTED, app_icon, avatar_pixmap, initials
from gui_update import UpdateButton

ROLE = Qt.UserRole
IS_WIN = sys.platform == "win32"


class MainWindow(QMainWindow):
    logged_out = Signal()
    auth_failed = Signal(str)

    def __init__(self, api: Api, settings):
        super().__init__()
        self.api = api
        self.s = settings
        self.setWindowTitle("Беседа")
        self.setWindowIcon(app_icon())
        self.resize(1280, 760)
        geo = self.s.value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)

        # ---- состояние ----
        self.me: dict = {}
        self.guilds: dict[int, dict] = {}          # id -> {..., channels: {cid: ch}, members: set}
        self.users: dict[int, dict] = {}
        self.voice: dict[int, dict] = {}           # uid -> voice_state
        self.msgs: dict[int, list] = {}            # cid -> сообщения
        self.more: dict[int, bool] = {}
        self.unread: set[int] = set()
        self.typing: dict[int, dict[int, float]] = {}
        self.cur_guild: int | None = None
        self.cur_channel: int | None = None
        self.want_voice: int | None = None         # голосовой канал, в котором мы хотим быть
        self.voice_items: dict[int, QTreeWidgetItem] = {}
        self._speaking: set[int] = set()
        self._voice_since = 0.0
        self.max_upload_mb = 50
        self._uploading = False

        # ---- сеть и звук ----
        self.images = ImageCache(api, self)
        self.engine = VoiceEngine()
        apply_audio_settings(self.engine, self.s)
        for k in self.s.allKeys():
            if k.startswith("uservol/"):
                try:
                    self.engine.user_volume[int(k.split("/", 1)[1])] = float(self.s.value(k))
                except (TypeError, ValueError):
                    pass
        self.self_mute = False
        self.self_deaf = False
        self.gw = Gateway(api.ws_url, api.token, self, insecure_ssl=api.insecure_ssl)
        self.gw.event.connect(self.on_event)
        self.gw.offline.connect(self._offline)
        self.gw.auth_failed.connect(self.auth_failed)

        self._build()
        self._tray()
        run_bg(api.info, on_done=lambda i: setattr(self, "max_upload_mb", int(i.get("max_upload_mb", 50))))

        self.tick = QTimer(self)
        self.tick.setInterval(100)
        self.tick.timeout.connect(self._on_tick)
        self.tick.start()
        self.ptt_timer = QTimer(self)
        self.ptt_timer.setInterval(15)
        self.ptt_timer.timeout.connect(self._poll_ptt)
        self.ptt_timer.start()
        self.gw.start()
        self._set_status("Подключение…")

    # ================================================================ интерфейс
    def _build(self):
        root = QWidget()
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        # --- полоса серверов ---
        gb = QWidget()
        gb.setObjectName("guildBar")
        gb.setAttribute(Qt.WA_StyledBackground, True)
        gb.setFixedWidth(72)
        gbl = QVBoxLayout(gb)
        gbl.setContentsMargins(0, 12, 0, 12)
        gbl.setSpacing(8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        self.guild_lay = QVBoxLayout(inner)
        self.guild_lay.setContentsMargins(12, 0, 12, 0)
        self.guild_lay.setSpacing(8)
        self.guild_lay.addStretch(1)
        scroll.setWidget(inner)
        gbl.addWidget(scroll, 1)
        add = QPushButton("+")
        add.setObjectName("guildAdd")
        add.setFixedSize(48, 48)
        add.setToolTip("Создать сервер или войти по приглашению")
        add.clicked.connect(self._guild_add_menu)
        gbl.addWidget(add, 0, Qt.AlignHCenter)
        self.guild_buttons: dict[int, QPushButton] = {}
        h.addWidget(gb)

        # --- каналы ---
        side = QWidget()
        side.setObjectName("sidePanel")
        side.setAttribute(Qt.WA_StyledBackground, True)
        side.setFixedWidth(250)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        head = QWidget()
        head.setObjectName("headerBar")
        head.setAttribute(Qt.WA_StyledBackground, True)
        head.setFixedHeight(48)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(4, 0, 6, 0)
        self.guild_title = QLabel("")
        self.guild_title.setObjectName("header")
        self.guild_menu_btn = QPushButton("⋮")
        self.guild_menu_btn.setObjectName("iconBtn")
        self.guild_menu_btn.setToolTip("Меню сервера")
        self.guild_menu_btn.clicked.connect(self._guild_menu)
        hl.addWidget(self.guild_title, 1)
        hl.addWidget(self.guild_menu_btn)
        sl.addWidget(head)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.tree.setRootIsDecorated(False)
        self.tree.setFocusPolicy(Qt.NoFocus)
        self.tree.itemClicked.connect(self._tree_clicked)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        sl.addWidget(self.tree, 1)

        # голосовая панель
        self.vpanel = QWidget()
        self.vpanel.setObjectName("voicePanel")
        self.vpanel.setAttribute(Qt.WA_StyledBackground, True)
        vl = QHBoxLayout(self.vpanel)
        vl.setContentsMargins(10, 8, 6, 8)
        vcol = QVBoxLayout()
        vcol.setSpacing(0)
        self.v_status = QLabel("")
        self.v_where = QLabel("")
        self.v_where.setObjectName("muted")
        vcol.addWidget(self.v_status)
        vcol.addWidget(self.v_where)
        vl.addLayout(vcol, 1)
        hang = QPushButton("✆")
        hang.setObjectName("iconBtn")
        hang.setToolTip("Отключиться от голоса")
        hang.setStyleSheet("color: #da373c;")
        hang.clicked.connect(self.leave_voice)
        vl.addWidget(hang)
        self.vpanel.hide()
        sl.addWidget(self.vpanel)

        # панель пользователя
        up = QWidget()
        up.setObjectName("userPanel")
        up.setAttribute(Qt.WA_StyledBackground, True)
        up.setFixedHeight(54)
        ul = QHBoxLayout(up)
        ul.setContentsMargins(8, 0, 6, 0)
        ul.setSpacing(4)
        self.my_avatar = QLabel()
        self.my_name = QLabel("")
        self.my_name.setStyleSheet("font-weight: 600;")
        self.my_status = QLabel("")
        self.my_status.setObjectName("muted")
        self.my_status.setStyleSheet("font-size: 8.5pt;")
        nc = QVBoxLayout()
        nc.setSpacing(0)
        nc.addWidget(self.my_name)
        nc.addWidget(self.my_status)
        ul.addWidget(self.my_avatar)
        ul.addLayout(nc, 1)
        self.mute_btn = QPushButton("🎙")
        self.deaf_btn = QPushButton("🎧")
        self.set_btn = QPushButton("⚙")
        for b, tip in ((self.mute_btn, "Выключить микрофон"), (self.deaf_btn, "Выключить звук"),
                       (self.set_btn, "Настройки")):
            b.setObjectName("iconBtn")
            b.setToolTip(tip)
            ul.addWidget(b)
        self.mute_btn.setCheckable(True)
        self.deaf_btn.setCheckable(True)
        self.mute_btn.clicked.connect(self.toggle_mute)
        self.deaf_btn.clicked.connect(self.toggle_deaf)
        self.set_btn.clicked.connect(self.open_settings)
        sl.addWidget(up)

        # кнопка версии / обновления из GitHub
        self.update_btn = UpdateButton(
            self,
            can_quit=lambda: "Сначала отключитесь от голосового канала." if self.engine.in_voice else "",
            last_check_get=lambda: float(self.s.value("update/last_check", 0) or 0),
            last_check_set=lambda t: self.s.setValue("update/last_check", t),
        )
        sl.addWidget(self.update_btn)

        h.addWidget(side)

        # --- чат ---
        self.chat = ChatPanel(self.images)
        v = self.chat.view
        v.load_more.connect(self._load_more)
        v.action.connect(self._msg_action)
        v.open_attachment.connect(self._open_attachment)
        v.save_attachment.connect(self._save_attachment)
        self.chat.composer.send.connect(self._send)
        self.chat.composer.typing.connect(
            lambda: self.cur_channel and self.gw.send({"type": "typing", "channel_id": self.cur_channel}))
        h.addWidget(self.chat, 1)

        # --- участники ---
        mp = QWidget()
        mp.setObjectName("memberPanel")
        mp.setAttribute(Qt.WA_StyledBackground, True)
        mp.setFixedWidth(230)
        ml = QVBoxLayout(mp)
        ml.setContentsMargins(8, 12, 8, 8)
        self.members = QListWidget()
        self.members.setFocusPolicy(Qt.NoFocus)
        self.members.setSelectionMode(QListWidget.NoSelection)
        self.members.setContextMenuPolicy(Qt.CustomContextMenu)
        self.members.customContextMenuRequested.connect(self._member_menu)
        ml.addWidget(self.members)
        h.addWidget(mp)

        self.setCentralWidget(root)
        self.statusBar().setStyleSheet("background: #1e1f22; color: #949ba4;")
        self.statusBar().hide()

    def _tray(self):
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(app_icon(), self)
            self.tray.setToolTip("Беседа")
            m = QMenu()
            m.addAction("Открыть", self._raise)
            m.addAction("Выход", self.quit_app)
            self.tray.setContextMenu(m)
            self.tray.activated.connect(lambda r: self._raise() if r == QSystemTrayIcon.Trigger else None)
            self.tray.messageClicked.connect(self._raise)
            self.tray.show()

    def _raise(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def flash(self, text: str, error: bool = False):
        sb = self.statusBar()
        sb.setStyleSheet(f"background: {'#5c2326' if error else '#1e1f22'}; color: {'#fff' if error else MUTED};")
        sb.show()
        sb.showMessage(text, 6000)
        QTimer.singleShot(6100, lambda: sb.hide() if not sb.currentMessage() else None)

    def _set_status(self, text: str):
        self.my_status.setText(text)

    # ================================================================ события сервера
    def on_event(self, ev: dict):
        fn = getattr(self, "ev_" + str(ev.get("type")), None)
        if fn:
            fn(ev)

    def ev_ready(self, ev):
        self.me = ev["me"]
        self.users = {u["id"]: u for u in ev["users"]}
        self.guilds = {}
        for g in ev["guilds"]:
            self._add_guild(g)
        self.voice = {v["user_id"]: v for v in ev["voice_states"]}
        self.msgs.clear()
        self.more.clear()
        self._set_status("В сети")
        self.my_name.setText(self.me["display_name"])
        self.my_avatar.setPixmap(avatar_pixmap(self.me["display_name"], 32, key=str(self.me["id"])))
        self.chat.view.my_id = self.me["id"]
        self.chat.view.my_name = self.me["display_name"]
        self._render_guilds()
        last = int(self.s.value("last_guild", 0) or 0)
        gid = self.cur_guild if self.cur_guild in self.guilds else (last if last in self.guilds else None)
        if gid is None and self.guilds:
            gid = next(iter(self.guilds))
        self.select_guild(gid, keep_channel=True)
        # после переподключения сервер забыл нашу голосовую сессию — заходим снова
        if self.want_voice and self._channel(self.want_voice):
            self.join_voice(self.want_voice, silent=True)
        elif self.want_voice:
            self._voice_gone()

    def _offline(self, reason):
        self._set_status("Нет связи, переподключение…")
        if self.engine.in_voice:
            self.engine.disconnect()

    def ev_error(self, ev):
        self.flash(ev.get("message", "Ошибка"), error=True)

    def ev_system_notice(self, ev):
        """Объявление администратора из веб-админки."""
        text = ev.get("text", "")
        sender = ev.get("from", "Администратор")
        if self.tray and not self.isActiveWindow():
            self.tray.showMessage(f"Объявление · {sender}", text, QSystemTrayIcon.Information, 10000)
        QApplication.alert(self)
        box = QMessageBox(QMessageBox.Information, "Объявление", text, QMessageBox.Ok, self)
        box.setInformativeText(f"— {sender}")
        box.setWindowModality(Qt.NonModal)
        box.setAttribute(Qt.WA_DeleteOnClose)
        box.show()

    def ev_guild_add(self, ev):
        for u in ev.get("users", []):
            self.users[u["id"]] = u
        for v in ev.get("voice_states", []):
            self.voice[v["user_id"]] = v
        self._add_guild(ev["guild"])
        self._render_guilds()
        self.select_guild(ev["guild"]["id"])

    def ev_guild_remove(self, ev):
        gid = ev["guild_id"]
        g = self.guilds.pop(gid, None)
        if g and self.want_voice in g["channels"]:
            self._voice_gone()
        self._render_guilds()
        if self.cur_guild == gid:
            self.select_guild(next(iter(self.guilds), None))

    def ev_guild_update(self, ev):
        g = self.guilds.get(ev["guild_id"])
        if g:
            g["name"], g["invite"] = ev["name"], ev["invite"]
            g["owner_id"] = ev.get("owner_id", g["owner_id"])
            self._render_guilds()
            if self.cur_guild == g["id"]:
                self.guild_title.setText(g["name"])
                self.chat.view.can_moderate = self._is_owner(g["id"])
                self._render_members()

    def ev_member_join(self, ev):
        u = ev["user"]
        self.users[u["id"]] = u
        g = self.guilds.get(ev["guild_id"])
        if g:
            g["members"].add(u["id"])
            if self.cur_guild == g["id"]:
                self._render_members()

    def ev_member_leave(self, ev):
        g = self.guilds.get(ev["guild_id"])
        if g:
            g["members"].discard(ev["user_id"])
            if self.cur_guild == g["id"]:
                self._render_members()

    def ev_channel_create(self, ev):
        ch = ev["channel"]
        g = self.guilds.get(ch["guild_id"])
        if g:
            g["channels"][ch["id"]] = ch
            if self.cur_guild == g["id"]:
                self._render_tree()

    ev_channel_update = ev_channel_create

    def ev_channel_delete(self, ev):
        g = self.guilds.get(ev["guild_id"])
        if g:
            g["channels"].pop(ev["channel_id"], None)
        self.msgs.pop(ev["channel_id"], None)
        if self.want_voice == ev["channel_id"]:
            self._voice_gone()
        if self.cur_channel == ev["channel_id"]:
            self.cur_channel = None
            self.select_guild(self.cur_guild)
        elif self.cur_guild == ev["guild_id"]:
            self._render_tree()

    def ev_history(self, ev):
        cid = ev["channel_id"]
        older = bool(ev.get("before"))
        cur = self.msgs.get(cid, []) if older else []
        known = {m["id"] for m in cur}
        self.msgs[cid] = [m for m in ev["messages"] if m["id"] not in known] + cur
        self.more[cid] = ev["has_more"]
        if cid == self.cur_channel:
            self.chat.view.set_messages(self.msgs[cid], self.more[cid], prepend=older)

    def ev_message_create(self, ev):
        m = ev["message"]
        cid = m["channel_id"]
        if cid in self.msgs:
            self.msgs[cid].append(m)
        self.typing.get(cid, {}).pop(m["author_id"], None)
        visible = cid == self.cur_channel and self.isActiveWindow()
        if cid == self.cur_channel and cid in self.msgs:
            self.chat.view.append(m)
        if m["author_id"] == self.me.get("id"):
            return
        if cid != self.cur_channel:
            self.unread.add(cid)
            self._render_tree()
            self._render_guild_unread()
        if not visible:
            QApplication.alert(self)
            if self._mentions_me(m["content"]) and self.tray:
                ch = self._channel(cid)
                self.tray.showMessage(f'{m["author_name"]} в #{ch["name"] if ch else "?"}',
                                      m["content"][:200], QSystemTrayIcon.Information, 5000)

    def ev_message_update(self, ev):
        m = ev["message"]
        lst = self.msgs.get(m["channel_id"])
        if lst is not None:
            for i, x in enumerate(lst):
                if x["id"] == m["id"]:
                    lst[i] = m
            if m["channel_id"] == self.cur_channel:
                self.chat.view.set_messages(lst, self.more.get(m["channel_id"], False), prepend=True)

    def ev_message_delete(self, ev):
        lst = self.msgs.get(ev["channel_id"])
        if lst is not None:
            self.msgs[ev["channel_id"]] = [x for x in lst if x["id"] != ev["message_id"]]
            if ev["channel_id"] == self.cur_channel:
                self.chat.view.set_messages(self.msgs[ev["channel_id"]], self.more.get(ev["channel_id"], False),
                                            prepend=True)

    def ev_typing(self, ev):
        self.typing.setdefault(ev["channel_id"], {})[ev["user_id"]] = time.time()

    def ev_presence(self, ev):
        u = self.users.get(ev["user_id"])
        if u:
            u["online"] = ev["online"]
            self._render_members()

    def ev_user_update(self, ev):
        u = ev["user"]
        self.users[u["id"]] = u
        if u["id"] == self.me.get("id"):
            self.me.update(u)
            self.my_name.setText(u["display_name"])
            self.my_avatar.setPixmap(avatar_pixmap(u["display_name"], 32, key=str(u["id"])))
            self.chat.view.my_name = u["display_name"]
        self._render_members()
        self._render_tree()

    def ev_voice_state(self, ev):
        uid = ev["user_id"]
        old = self.voice.get(uid)
        if ev.get("channel_id"):
            self.voice[uid] = ev
        else:
            self.voice.pop(uid, None)
        mine = self.want_voice
        if uid != self.me.get("id") and mine and self.engine.in_voice:
            was_here = old and old.get("channel_id") == mine
            now_here = ev.get("channel_id") == mine
            if now_here and not was_here:
                self.engine.play_cue("join")
            elif was_here and not now_here:
                self.engine.play_cue("leave")
        self._render_tree()

    def ev_voice_ready(self, ev):
        if ev["channel_id"] != self.want_voice:
            return
        host = ev.get("udp_host") or self.api.host
        key = base64.b64decode(ev["key"])
        self.engine.muted = self.self_mute or self.self_deaf
        self.engine.deafened = self.self_deaf
        self._voice_since = time.time()

        def connect():
            self.engine.connect(host, int(ev["udp_port"]), int(ev["ssrc"]), key)

        def ok(_):
            self.engine.play_cue("join")
            self._render_voice_panel()

        run_bg(connect, on_done=ok, on_fail=lambda e: self.flash(f"Голос: {e}", error=True))
        self._render_voice_panel()

    def ev_voice_closed(self, ev):
        self._voice_gone()

    # ================================================================ модель
    def _add_guild(self, g: dict):
        self.guilds[g["id"]] = {
            "id": g["id"], "name": g["name"], "owner_id": g["owner_id"], "invite": g["invite"],
            "channels": {c["id"]: c for c in g["channels"]}, "members": set(g["members"]),
        }

    def _channel(self, cid):
        for g in self.guilds.values():
            if cid in g["channels"]:
                return g["channels"][cid]
        return None

    def _is_owner(self, gid=None) -> bool:
        g = self.guilds.get(gid or self.cur_guild)
        return bool(g and (g["owner_id"] == self.me.get("id") or self.me.get("is_admin")))

    def _mentions_me(self, text: str) -> bool:
        t = text.lower()
        return any(f"@{n}".lower() in t for n in (self.me.get("display_name", "~"), self.me.get("username", "~"))) \
            or "@все" in t or "@everyone" in t

    def _uname(self, uid) -> str:
        u = self.users.get(uid)
        return u["display_name"] if u else f"#{uid}"

    # ================================================================ отрисовка
    def _render_guilds(self):
        for b in self.guild_buttons.values():
            b.deleteLater()
        self.guild_buttons = {}
        for gid, g in self.guilds.items():
            b = QPushButton(initials(g["name"]))
            b.setObjectName("guild")
            b.setCheckable(True)
            b.setFixedSize(48, 48)
            b.setToolTip(g["name"])
            b.setChecked(gid == self.cur_guild)
            b.clicked.connect(lambda _=False, gid=gid: self.select_guild(gid))
            self.guild_lay.insertWidget(self.guild_lay.count() - 1, b)
            self.guild_buttons[gid] = b
        self._render_guild_unread()

    def _render_guild_unread(self):
        for gid, b in self.guild_buttons.items():
            has = any(c in self.unread for c in self.guilds[gid]["channels"])
            if b.property("unread") != has:
                b.setProperty("unread", has)
                b.style().unpolish(b)
                b.style().polish(b)

    def select_guild(self, gid, keep_channel=False):
        self.cur_guild = gid
        for x, b in self.guild_buttons.items():
            b.setChecked(x == gid)
        g = self.guilds.get(gid)
        self.guild_title.setText(g["name"] if g else "Нет серверов")
        self.guild_menu_btn.setVisible(g is not None)
        if g:
            self.s.setValue("last_guild", gid)
        target = None
        if g:
            if keep_channel and self.cur_channel in g["channels"]:
                target = self.cur_channel
            else:
                last = int(self.s.value(f"last_channel/{gid}", 0) or 0)
                texts = sorted((c for c in g["channels"].values() if c["kind"] == "text"),
                               key=lambda c: (c["position"], c["id"]))
                target = last if last in g["channels"] and g["channels"][last]["kind"] == "text" else \
                    (texts[0]["id"] if texts else None)
        self.open_channel(target, force=True)
        self._render_members()
        if not g:
            self.chat.placeholder.setText("Создайте сервер или войдите по приглашению — кнопка «+» слева")

    def open_channel(self, cid, force=False):
        if cid == self.cur_channel and not force:
            return
        self.cur_channel = cid
        ch = self._channel(cid) if cid else None
        if not ch:
            self.cur_channel = None
            self.chat.set_active(False)
            self.chat.title.setText("")
            self._render_tree()
            return
        self.unread.discard(cid)
        self.s.setValue(f"last_channel/{ch['guild_id']}", cid)
        self.chat.set_active(True)
        self.chat.title.setText(f"#  {ch['name']}")
        self.chat.composer.set_channel_name(ch["name"])
        self.chat.view.can_moderate = self._is_owner(ch["guild_id"])
        if cid in self.msgs:
            self.chat.view.set_messages(self.msgs[cid], self.more.get(cid, False))
        else:
            self.chat.view.set_messages([], False)
            self.gw.send({"type": "history", "channel_id": cid})
        self._render_tree()
        self._render_guild_unread()
        self.chat.composer.edit.setFocus()

    def _render_tree(self):
        self.tree.clear()
        self.voice_items = {}
        g = self.guilds.get(self.cur_guild)
        if not g:
            return
        chans = sorted(g["channels"].values(), key=lambda c: (c["position"], c["id"]))
        bold = QFont()
        bold.setBold(True)
        for kind, title in (("text", "ТЕКСТОВЫЕ КАНАЛЫ"), ("voice", "ГОЛОСОВЫЕ КАНАЛЫ")):
            cat = QTreeWidgetItem([title])
            cat.setData(0, ROLE, ("cat", kind))
            cat.setFlags(Qt.ItemIsEnabled)
            f = QFont()
            f.setPointSizeF(8)
            f.setBold(True)
            cat.setFont(0, f)
            self.tree.addTopLevelItem(cat)
            for c in (c for c in chans if c["kind"] == kind):
                prefix = "#  " if kind == "text" else "🔊 "
                it = QTreeWidgetItem([prefix + c["name"]])
                it.setData(0, ROLE, (kind, c["id"]))
                if c["id"] in self.unread:
                    it.setFont(0, bold)
                    it.setForeground(0, QBrush(QColor("white")))
                if kind == "voice" and c["id"] == self.want_voice:
                    it.setForeground(0, QBrush(QColor(GREEN)))
                cat.addChild(it)
                if c["id"] == self.cur_channel:
                    self.tree.setCurrentItem(it)
                if kind == "voice":
                    for uid, st in sorted(self.voice.items(), key=lambda kv: self._uname(kv[0]).lower()):
                        if st.get("channel_id") != c["id"]:
                            continue
                        flags = (" 🔇" if st.get("mute") and not st.get("deaf") else "") + (" 🔕" if st.get("deaf") else "")
                        u = QTreeWidgetItem([self._uname(uid) + flags])
                        u.setData(0, ROLE, ("user", uid))
                        u.setIcon(0, QIcon(avatar_pixmap(self._uname(uid), 20, key=str(uid))))
                        it.addChild(u)
                        self.voice_items[uid] = u
            cat.setExpanded(True)
        self.tree.expandAll()
        self._speaking = set()
        self._update_speaking()

    def _render_members(self):
        self.members.clear()
        g = self.guilds.get(self.cur_guild)
        if not g:
            return
        on = sorted((self.users[u] for u in g["members"] if u in self.users and self.users[u].get("online")),
                    key=lambda u: u["display_name"].lower())
        off = sorted((self.users[u] for u in g["members"] if u in self.users and not self.users[u].get("online")),
                     key=lambda u: u["display_name"].lower())
        for title, group, online in ((f"В СЕТИ — {len(on)}", on, True), (f"НЕ В СЕТИ — {len(off)}", off, False)):
            if not group:
                continue
            hdr = QListWidgetItem(title)
            hdr.setFlags(Qt.NoItemFlags)
            f = QFont()
            f.setPointSizeF(8)
            f.setBold(True)
            hdr.setFont(f)
            self.members.addItem(hdr)
            for u in group:
                crown = "  👑" if u["id"] == g["owner_id"] else ""
                it = QListWidgetItem(QIcon(avatar_pixmap(u["display_name"], 32, key=str(u["id"]))),
                                     u["display_name"] + crown)
                it.setData(ROLE, u["id"])
                it.setToolTip(f'@{u["username"]}')
                if not online:
                    it.setForeground(QBrush(QColor("#5c5f66")))
                self.members.addItem(it)

    def _render_voice_panel(self):
        if not self.want_voice:
            self.vpanel.hide()
            return
        ch = self._channel(self.want_voice)
        g = self.guilds.get(ch["guild_id"]) if ch else None
        self.vpanel.show()
        if self.engine.udp_ok:
            self.v_status.setText("● Голос подключён")
            self.v_status.setStyleSheet(f"color: {GREEN}; font-weight: 600;")
        elif time.time() - self._voice_since > 6 and self.engine.in_voice:
            self.v_status.setText("● Нет UDP-связи")
            self.v_status.setStyleSheet("color: #da373c; font-weight: 600;")
            self.v_status.setToolTip("Сервер не отвечает по UDP. Проверьте, открыт ли порт голоса на сервере (ufw).")
        else:
            self.v_status.setText("● Подключение…")
            self.v_status.setStyleSheet("color: #f0b232; font-weight: 600;")
        self.v_where.setText(f'{ch["name"] if ch else "?"} / {g["name"] if g else "?"}')

    def _update_speaking(self):
        now = set(self.engine.speaking_users())
        if self.engine.self_speaking and self.me:
            now.add(self.me["id"])
        if now == self._speaking:
            return
        for uid, it in self.voice_items.items():
            try:
                if uid in now:
                    it.setForeground(0, QBrush(QColor(GREEN)))
                    f = it.font(0)
                    f.setBold(True)
                    it.setFont(0, f)
                else:
                    it.setForeground(0, QBrush(QColor(MUTED)))
                    f = it.font(0)
                    f.setBold(False)
                    it.setFont(0, f)
            except RuntimeError:
                pass
        self._speaking = now

    def _on_tick(self):
        self._update_speaking()
        if self.want_voice:
            self._render_voice_panel()
        # «печатает…»
        cid = self.cur_channel
        t = time.time()
        names = [self._uname(uid) for uid, ts in self.typing.get(cid, {}).items() if t - ts < 6] if cid else []
        if not names:
            txt = ""
        elif len(names) == 1:
            txt = f"{names[0]} печатает…"
        elif len(names) <= 3:
            txt = f"{', '.join(names)} печатают…"
        else:
            txt = "Несколько человек печатают…"
        if self.chat.typing.text() != txt:
            self.chat.typing.setText(txt)

    def _poll_ptt(self):
        if not (IS_WIN and self.engine.ptt_mode):
            return
        vk = int(self.s.value("audio/ptt_vk", 0) or 0)
        if vk:
            self.engine.ptt_pressed = bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)

    # ================================================================ действия: каналы
    def _tree_clicked(self, item, _col):
        data = item.data(0, ROLE)
        if not data:
            return
        kind, ident = data
        if kind == "text":
            self.open_channel(ident)
        elif kind == "voice":
            self.join_voice(ident)

    def _tree_menu(self, pos):
        item = self.tree.itemAt(pos)
        m = QMenu(self)
        data = item.data(0, ROLE) if item else None
        owner = self._is_owner()
        if data and data[0] == "user":
            self._user_actions(m, data[1])
        elif data and data[0] in ("text", "voice"):
            if data[0] == "voice":
                m.addAction("Подключиться", lambda: self.join_voice(data[1]))
            if owner:
                m.addAction("Переименовать канал", lambda: self._rename_channel(data[1]))
                m.addAction("Удалить канал", lambda: self._delete_channel(data[1]))
        if owner:
            if not m.isEmpty():
                m.addSeparator()
            m.addAction("Создать текстовый канал", lambda: self._create_channel("text"))
            m.addAction("Создать голосовой канал", lambda: self._create_channel("voice"))
        if not m.isEmpty():
            m.exec(self.tree.viewport().mapToGlobal(pos))

    def _create_channel(self, kind):
        name, ok = QInputDialog.getText(self, "Новый канал",
                                        "Название текстового канала:" if kind == "text" else "Название голосового канала:")
        if ok and name.strip():
            self.gw.send({"type": "channel_create", "guild_id": self.cur_guild, "name": name.strip(), "kind": kind})

    def _rename_channel(self, cid):
        ch = self._channel(cid)
        name, ok = QInputDialog.getText(self, "Переименовать канал", "Новое название:", text=ch["name"] if ch else "")
        if ok and name.strip():
            self.gw.send({"type": "channel_update", "channel_id": cid, "name": name.strip()})

    def _delete_channel(self, cid):
        ch = self._channel(cid)
        if ch and QMessageBox.question(self, "Удалить канал",
                                       f"Удалить канал «{ch['name']}» вместе со всей историей?") == QMessageBox.Yes:
            self.gw.send({"type": "channel_delete", "channel_id": cid})

    # ================================================================ действия: серверы
    def _guild_add_menu(self):
        m = QMenu(self)
        m.addAction("Создать сервер", self._create_guild)
        m.addAction("Войти по приглашению", self._join_guild)
        m.exec(self.cursor().pos())

    def _create_guild(self):
        name, ok = QInputDialog.getText(self, "Новый сервер", "Название сервера:")
        if ok and name.strip():
            self.gw.send({"type": "guild_create", "name": name.strip()})

    def _join_guild(self):
        code, ok = QInputDialog.getText(self, "Вход по приглашению", "Код приглашения:")
        if ok and code.strip():
            self.gw.send({"type": "guild_join", "invite": code.strip()})

    def _guild_menu(self):
        g = self.guilds.get(self.cur_guild)
        if not g:
            return
        m = QMenu(self)
        m.addAction("Пригласить людей (скопировать код)", self._copy_invite)
        if self._is_owner():
            m.addAction("Создать текстовый канал", lambda: self._create_channel("text"))
            m.addAction("Создать голосовой канал", lambda: self._create_channel("voice"))
            m.addAction("Переименовать сервер", self._rename_guild)
            m.addAction("Новый код приглашения (старый перестанет работать)",
                        lambda: self.gw.send({"type": "guild_update", "guild_id": g["id"], "regen_invite": True}))
            m.addSeparator()
            m.addAction("Удалить сервер", self._delete_guild)
        else:
            m.addSeparator()
            m.addAction("Покинуть сервер", self._leave_guild)
        m.exec(self.guild_menu_btn.mapToGlobal(self.guild_menu_btn.rect().bottomLeft()))

    def _copy_invite(self):
        g = self.guilds.get(self.cur_guild)
        QGuiApplication.clipboard().setText(g["invite"])
        QMessageBox.information(self, "Приглашение",
                                f"Код приглашения скопирован:\n\n{g['invite']}\n\n"
                                f"Отправьте его другу вместе с адресом сервера:\n{self.api.base}")

    def _rename_guild(self):
        g = self.guilds.get(self.cur_guild)
        name, ok = QInputDialog.getText(self, "Переименовать сервер", "Новое название:", text=g["name"])
        if ok and name.strip():
            self.gw.send({"type": "guild_update", "guild_id": g["id"], "name": name.strip()})

    def _delete_guild(self):
        g = self.guilds.get(self.cur_guild)
        name, ok = QInputDialog.getText(self, "Удалить сервер",
                                        f"Это удалит все каналы и сообщения.\nВведите название «{g['name']}» для подтверждения:")
        if ok and name.strip() == g["name"]:
            self.gw.send({"type": "guild_delete", "guild_id": g["id"]})

    def _leave_guild(self):
        g = self.guilds.get(self.cur_guild)
        if QMessageBox.question(self, "Покинуть сервер", f"Покинуть «{g['name']}»?") == QMessageBox.Yes:
            self.gw.send({"type": "guild_leave", "guild_id": g["id"]})

    # ================================================================ действия: участники
    def _member_menu(self, pos):
        item = self.members.itemAt(pos)
        uid = item.data(ROLE) if item else None
        if not uid:
            return
        m = QMenu(self)
        self._user_actions(m, uid)
        if not m.isEmpty():
            m.exec(self.members.viewport().mapToGlobal(pos))

    def _user_actions(self, m: QMenu, uid: int):
        u = self.users.get(uid, {})
        if uid != self.me.get("id"):
            m.addAction("Упомянуть", lambda: self.chat.composer.insert_text(f"@{u.get('display_name', '')} "))
            # громкость конкретного человека (как фейдер на пульте)
            w = QWidget()
            hl = QHBoxLayout(w)
            hl.setContentsMargins(12, 4, 12, 4)
            lbl = QLabel()
            sl = QSlider(Qt.Horizontal)
            sl.setRange(0, 200)
            sl.setFixedWidth(160)
            cur = int(self.engine.user_volume.get(uid, 1.0) * 100)
            sl.setValue(cur)
            lbl.setText(f"Громкость {cur}%")

            def changed(v, uid=uid, lbl=lbl):
                self.engine.user_volume[uid] = v / 100
                self.s.setValue(f"uservol/{uid}", v / 100)
                lbl.setText(f"Громкость {v}%")

            sl.valueChanged.connect(changed)
            hl.addWidget(lbl)
            hl.addWidget(sl)
            act = QWidgetAction(m)
            act.setDefaultWidget(w)
            m.addAction(act)
            muted = uid in self.engine.local_mutes
            m.addAction("Включить звук для меня" if muted else "Заглушить для меня",
                        lambda: self.engine.local_mutes.symmetric_difference_update({uid}))
            if self._is_owner() and uid != self.guilds[self.cur_guild]["owner_id"]:
                m.addSeparator()
                def kick():
                    if QMessageBox.question(self, "Выгнать", f"Выгнать {u.get('display_name')} с сервера?") == QMessageBox.Yes:
                        self.gw.send({"type": "guild_kick", "guild_id": self.cur_guild, "user_id": uid})
                m.addAction(f"Выгнать {u.get('display_name', '')}", kick)

    # ================================================================ действия: сообщения
    def _load_more(self):
        cid = self.cur_channel
        lst = self.msgs.get(cid)
        if cid and lst:
            self.gw.send({"type": "history", "channel_id": cid, "before": lst[0]["id"]})

    def _send(self, text: str, files: list):
        cid = self.cur_channel
        if not cid:
            return
        if not self.gw.is_online:
            self.flash("Нет связи с сервером — сообщение не отправлено", error=True)
            self.chat.composer.insert_text(text)
            return
        if not files:
            self.gw.send({"type": "send", "channel_id": cid, "content": text})
            return
        limit = self.max_upload_mb * 1024 * 1024
        big = [os.path.basename(p) for p in files if os.path.getsize(p) > limit]
        if big:
            self.flash(f"Файл больше {self.max_upload_mb} МБ: {', '.join(big)}", error=True)
            return
        total = sum(os.path.getsize(p) for p in files) or 1

        def upload_all(progress):
            ids, done = [], 0
            for p in files:
                base = done
                r = self.api.upload(p, progress=lambda a, b, base=base: progress(base + a, total))
                ids.append(r["id"])
                done += os.path.getsize(p)
            return ids

        def ok(ids):
            self.chat.composer.set_status("")
            self.gw.send({"type": "send", "channel_id": cid, "content": text, "files": ids})

        def fail(e):
            self.chat.composer.set_status("")
            self.flash(f"Не удалось загрузить: {e}", error=True)

        self.chat.composer.set_status("Загрузка… 0%")
        run_bg(upload_all, on_done=ok, on_fail=fail,
               on_progress=lambda a, b: self.chat.composer.set_status(f"Загрузка… {int(a * 100 / max(b, 1))}%"))

    def _msg_action(self, act: str, m: dict):
        if act == "copy":
            QGuiApplication.clipboard().setText(m["content"])
        elif act == "mention":
            self.chat.composer.insert_text(f"@{m['author_name']} ")
        elif act == "edit":
            text, ok = QInputDialog.getMultiLineText(self, "Редактировать сообщение", "Текст:", m["content"])
            if ok and text.strip() and text.strip() != m["content"]:
                self.gw.send({"type": "edit", "message_id": m["id"], "content": text.strip()})
        elif act == "delete":
            if QMessageBox.question(self, "Удалить", "Удалить сообщение?") == QMessageBox.Yes:
                self.gw.send({"type": "delete", "message_id": m["id"]})

    def _open_attachment(self, att: dict):
        p = self.images.path(att["id"], att["name"])
        if p.exists():
            open_local(str(p))
            return

        def fetch():
            p.write_bytes(self.api.fetch(att["id"]))
            return str(p)

        run_bg(fetch, on_done=open_local, on_fail=lambda e: self.flash(e, error=True))

    def _save_attachment(self, att: dict):
        folder = self.s.value("download_dir", os.path.join(os.path.expanduser("~"), "Downloads"))
        dest, _ = QFileDialog.getSaveFileName(self, "Сохранить файл", os.path.join(folder, att["name"]))
        if not dest:
            return
        self.s.setValue("download_dir", os.path.dirname(dest))
        self.flash(f"Скачивание {att['name']}…")
        run_bg(self.api.download, att["id"], dest,
               on_done=lambda p: self.flash(f"Сохранено: {p}"),
               on_fail=lambda e: self.flash(f"Ошибка скачивания: {e}", error=True),
               on_progress=lambda a, b: self.statusBar().showMessage(
                   f"Скачивание {att['name']}… {int(a * 100 / b)}%" if b else f"Скачивание… {a // 1024} КБ"))

    # ================================================================ голос
    def join_voice(self, cid, silent=False):
        if not silent and cid == self.want_voice and self.engine.in_voice:
            return
        if not self.gw.is_online:
            return
        if self.engine.in_voice:
            self.engine.disconnect()
        self.want_voice = cid
        self._voice_since = time.time()
        self.gw.send({"type": "voice_join", "channel_id": cid, "mute": self.self_mute or self.self_deaf,
                      "deaf": self.self_deaf})
        self._render_tree()
        self._render_voice_panel()

    def leave_voice(self):
        if self.want_voice:
            self.gw.send({"type": "voice_leave"})
            if self.engine.in_voice:
                self.engine.play_cue("leave")
                QTimer.singleShot(250, self._voice_gone)
            else:
                self._voice_gone()

    def _voice_gone(self):
        self.want_voice = None
        self.engine.disconnect()
        self.engine.stop_audio()
        self._render_voice_panel()
        self._render_tree()

    def toggle_mute(self):
        if self.self_deaf:
            # как в Discord: включение микрофона снимает и «оглушение»
            self.self_deaf = False
            self.self_mute = False
        else:
            self.self_mute = not self.self_mute
        self._apply_voice_flags("mute" if self.self_mute else "unmute")

    def toggle_deaf(self):
        self.self_deaf = not self.self_deaf
        self._apply_voice_flags("mute" if self.self_deaf else "unmute")

    def _apply_voice_flags(self, cue):
        self.engine.muted = self.self_mute or self.self_deaf
        self.engine.deafened = self.self_deaf
        self.mute_btn.setChecked(self.self_mute or self.self_deaf)
        self.mute_btn.setText("🔇" if (self.self_mute or self.self_deaf) else "🎙")
        self.mute_btn.setToolTip("Включить микрофон" if self.self_mute or self.self_deaf else "Выключить микрофон")
        self.deaf_btn.setChecked(self.self_deaf)
        self.deaf_btn.setToolTip("Включить звук" if self.self_deaf else "Выключить звук")
        if self.engine.in_voice:
            self.engine.play_cue(cue)
            self.gw.send({"type": "voice_update", "mute": self.self_mute or self.self_deaf, "deaf": self.self_deaf})

    # ================================================================ настройки и выход
    def open_settings(self):
        dlg = SettingsDialog(self.engine, self.s, self.api, self.me, self)
        dlg.logout_requested.connect(self._logout)
        dlg.display_name_changed.connect(
            lambda n: n and self.gw.send({"type": "set_display_name", "display_name": n}))
        dlg.exec()

    def _logout(self):
        run_bg(self.api.logout)
        self.s.remove("token")
        QTimer.singleShot(0, self.logged_out.emit)

    def shutdown(self):
        self.s.setValue("window/geometry", self.saveGeometry())
        self.tick.stop()
        self.ptt_timer.stop()
        self.gw.stop()
        self.engine.shutdown()
        if self.tray:
            self.tray.hide()

    def quit_app(self):
        self.close()

    def closeEvent(self, e):
        self.shutdown()
        e.accept()
        QApplication.quit()


__all__ = ["MainWindow", "cache_dir"]

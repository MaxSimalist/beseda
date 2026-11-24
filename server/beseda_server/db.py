"""SQLite-хранилище. Для 20–30 человек синхронного sqlite3 в WAL-режиме хватает с запасом."""
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    ukey TEXT UNIQUE NOT NULL,          -- username.casefold(): регистр не важен и для кириллицы
    display_name TEXT NOT NULL,
    pw_hash TEXT NOT NULL,
    created REAL NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions(
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS guilds(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    invite TEXT UNIQUE NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS members(
    guild_id INTEGER NOT NULL REFERENCES guilds(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    joined REAL NOT NULL,
    PRIMARY KEY(guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS channels(
    id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL REFERENCES guilds(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('text','voice')),
    position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    content TEXT NOT NULL,
    created REAL NOT NULL,
    edited REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, id);
CREATE TABLE IF NOT EXISTS files(
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    mime TEXT NOT NULL,
    created REAL NOT NULL,
    message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_files_message ON files(message_id);
"""


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, dk_hex = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def new_invite() -> str:
    return secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self):
        """Добавляет новые колонки в базы, созданные старой версией."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(users)")}
        if "banned" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN banned INTEGER NOT NULL DEFAULT 0")
        if "last_seen" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN last_seen REAL")
        if "is_mod" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN is_mod INTEGER NOT NULL DEFAULT 0")
        self.conn.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")

    # --- настройки, изменяемые из админки ---
    def get_setting(self, key: str, default=None):
        r = self.q1("SELECT value FROM settings WHERE key=?", (key,))
        return r[0] if r else default

    def set_setting(self, key: str, value):
        if value is None:
            self.ex("DELETE FROM settings WHERE key=?", (key,))
        else:
            self.ex("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)))

    # --- низкоуровневое ---
    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def q1(self, sql: str, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def ex(self, sql: str, args=()) -> int:
        with self.lock:
            return self.conn.execute(sql, args).lastrowid

    # --- пользователи ---
    def user_count(self) -> int:
        return self.q1("SELECT COUNT(*) FROM users")[0]

    def create_user(self, username: str, password: str) -> int:
        is_admin = 1 if self.user_count() == 0 else 0
        return self.ex(
            "INSERT INTO users(username, ukey, display_name, pw_hash, created, is_admin) VALUES(?,?,?,?,?,?)",
            (username, username.casefold(), username, hash_password(password), time.time(), is_admin),
        )

    def user_by_name(self, username: str):
        return self.q1("SELECT * FROM users WHERE ukey=?", (username.casefold(),))

    def user(self, user_id: int):
        return self.q1("SELECT * FROM users WHERE id=?", (user_id,))

    def set_display_name(self, user_id: int, name: str):
        self.ex("UPDATE users SET display_name=? WHERE id=?", (name, user_id))

    def set_password(self, user_id: int, password: str):
        self.ex("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(password), user_id))

    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        self.ex("INSERT INTO sessions(token, user_id, created) VALUES(?,?,?)", (token, user_id, time.time()))
        return token

    def user_by_token(self, token: str):
        if not token:
            return None
        return self.q1(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND u.banned=0", (token,)
        )

    def touch(self, user_id: int):
        self.ex("UPDATE users SET last_seen=? WHERE id=?", (time.time(), user_id))

    def delete_sessions_of(self, user_id: int):
        self.ex("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def set_banned(self, user_id: int, banned: bool):
        self.ex("UPDATE users SET banned=? WHERE id=?", (1 if banned else 0, user_id))
        if banned:
            self.delete_sessions_of(user_id)

    def set_admin(self, user_id: int, admin: bool):
        self.ex("UPDATE users SET is_admin=? WHERE id=?", (1 if admin else 0, user_id))

    def set_mod(self, user_id: int, mod: bool):
        self.ex("UPDATE users SET is_mod=? WHERE id=?", (1 if mod else 0, user_id))

    def admin_count(self) -> int:
        return self.q1("SELECT COUNT(*) FROM users WHERE is_admin=1 AND banned=0")[0]

    # --- выборки для админки ---
    def admin_users(self):
        return self.q("""
            SELECT u.id, u.username, u.display_name, u.created, u.is_admin, u.is_mod, u.banned, u.last_seen,
                   (SELECT COUNT(*) FROM members m WHERE m.user_id=u.id) AS guilds,
                   (SELECT COUNT(*) FROM messages x WHERE x.user_id=u.id) AS messages,
                   (SELECT COALESCE(SUM(size),0) FROM files f WHERE f.user_id=u.id) AS bytes
            FROM users u ORDER BY u.id""")

    def admin_guilds(self):
        return self.q("""
            SELECT g.id, g.name, g.owner_id, g.invite, g.created, o.display_name AS owner_name,
                   (SELECT COUNT(*) FROM members m WHERE m.guild_id=g.id) AS members,
                   (SELECT COUNT(*) FROM channels c WHERE c.guild_id=g.id) AS channels,
                   (SELECT COUNT(*) FROM messages x JOIN channels c ON c.id=x.channel_id WHERE c.guild_id=g.id) AS messages
            FROM guilds g LEFT JOIN users o ON o.id=g.owner_id ORDER BY g.id""")

    def set_owner(self, gid: int, uid: int):
        self.ex("UPDATE guilds SET owner_id=? WHERE id=?", (uid, gid))

    def totals(self) -> dict:
        r = self.q1("""SELECT (SELECT COUNT(*) FROM users), (SELECT COUNT(*) FROM guilds), (SELECT COUNT(*) FROM channels),
                              (SELECT COUNT(*) FROM messages), (SELECT COUNT(*) FROM files),
                              (SELECT COALESCE(SUM(size),0) FROM files),
                              (SELECT COUNT(*) FROM messages WHERE created>?)""", (time.time() - 86400,))
        return {"users": r[0], "guilds": r[1], "channels": r[2], "messages": r[3], "files": r[4],
                "files_bytes": r[5], "messages_24h": r[6]}

    def messages_per_day(self, days: int = 14) -> list[tuple[str, int]]:
        since = time.time() - days * 86400
        rows = self.q("""SELECT date(created, 'unixepoch', 'localtime') d, COUNT(*) FROM messages
                         WHERE created>? GROUP BY d ORDER BY d""", (since,))
        return [(r[0], r[1]) for r in rows]

    def search_messages(self, q: str, guild_id: int | None, user_id: int | None, limit: int = 100):
        sql = """SELECT m.id, m.content, m.created, m.edited, m.channel_id, c.name AS channel_name, c.guild_id,
                        g.name AS guild_name, m.user_id, u.display_name AS author,
                        (SELECT COUNT(*) FROM files f WHERE f.message_id=m.id) AS files
                 FROM messages m JOIN channels c ON c.id=m.channel_id JOIN guilds g ON g.id=c.guild_id
                 JOIN users u ON u.id=m.user_id WHERE 1=1"""
        args: list = []
        if q:
            sql += " AND m.content LIKE ? ESCAPE '\\'"
            args.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if guild_id:
            sql += " AND c.guild_id=?"
            args.append(guild_id)
        if user_id:
            sql += " AND m.user_id=?"
            args.append(user_id)
        sql += " ORDER BY m.id DESC LIMIT ?"
        args.append(limit)
        return self.q(sql, args)

    def admin_files(self, limit: int = 200):
        return self.q("""SELECT f.id, f.name, f.size, f.mime, f.created, f.message_id, u.display_name AS owner,
                                c.name AS channel_name, g.name AS guild_name
                         FROM files f JOIN users u ON u.id=f.user_id
                         LEFT JOIN messages m ON m.id=f.message_id LEFT JOIN channels c ON c.id=m.channel_id
                         LEFT JOIN guilds g ON g.id=c.guild_id ORDER BY f.size DESC LIMIT ?""", (limit,))

    def delete_session(self, token: str):
        self.ex("DELETE FROM sessions WHERE token=?", (token,))

    # --- серверы (гильдии) ---
    def create_guild(self, name: str, owner_id: int) -> int:
        now = time.time()
        with self.lock:
            gid = self.ex(
                "INSERT INTO guilds(name, owner_id, invite, created) VALUES(?,?,?,?)",
                (name, owner_id, new_invite(), now),
            )
            self.ex("INSERT INTO members(guild_id, user_id, joined) VALUES(?,?,?)", (gid, owner_id, now))
            self.ex("INSERT INTO channels(guild_id, name, kind, position) VALUES(?,?,?,?)", (gid, "общий", "text", 0))
            self.ex("INSERT INTO channels(guild_id, name, kind, position) VALUES(?,?,?,?)", (gid, "Голосовой", "voice", 1))
        return gid

    def guild(self, gid: int):
        return self.q1("SELECT * FROM guilds WHERE id=?", (gid,))

    def guild_by_invite(self, invite: str):
        return self.q1("SELECT * FROM guilds WHERE invite=?", (invite,))

    def guilds_of(self, user_id: int):
        return self.q(
            "SELECT g.* FROM guilds g JOIN members m ON m.guild_id=g.id WHERE m.user_id=? ORDER BY m.joined",
            (user_id,),
        )

    def rename_guild(self, gid: int, name: str):
        self.ex("UPDATE guilds SET name=? WHERE id=?", (name, gid))

    def regen_invite(self, gid: int) -> str:
        inv = new_invite()
        self.ex("UPDATE guilds SET invite=? WHERE id=?", (inv, gid))
        return inv

    def delete_guild(self, gid: int):
        self.ex("DELETE FROM guilds WHERE id=?", (gid,))

    def add_member(self, gid: int, user_id: int):
        self.ex("INSERT OR IGNORE INTO members(guild_id, user_id, joined) VALUES(?,?,?)", (gid, user_id, time.time()))

    def remove_member(self, gid: int, user_id: int):
        self.ex("DELETE FROM members WHERE guild_id=? AND user_id=?", (gid, user_id))

    def is_member(self, gid: int, user_id: int) -> bool:
        return self.q1("SELECT 1 FROM members WHERE guild_id=? AND user_id=?", (gid, user_id)) is not None

    def member_ids(self, gid: int) -> list[int]:
        return [r[0] for r in self.q("SELECT user_id FROM members WHERE guild_id=?", (gid,))]

    def users_sharing_guild(self, user_id: int) -> list[int]:
        """Все, кто состоит хотя бы в одном общем сервере с user_id (включая его самого)."""
        return [r[0] for r in self.q(
            "SELECT DISTINCT m2.user_id FROM members m1 JOIN members m2 ON m1.guild_id=m2.guild_id WHERE m1.user_id=?",
            (user_id,),
        )]

    # --- каналы ---
    def channels(self, gid: int):
        return self.q("SELECT * FROM channels WHERE guild_id=? ORDER BY kind DESC, position, id", (gid,))

    def channel(self, cid: int):
        return self.q1("SELECT * FROM channels WHERE id=?", (cid,))

    def create_channel(self, gid: int, name: str, kind: str) -> int:
        pos = self.q1("SELECT COALESCE(MAX(position),0)+1 FROM channels WHERE guild_id=?", (gid,))[0]
        return self.ex("INSERT INTO channels(guild_id, name, kind, position) VALUES(?,?,?,?)", (gid, name, kind, pos))

    def rename_channel(self, cid: int, name: str):
        self.ex("UPDATE channels SET name=? WHERE id=?", (name, cid))

    def delete_channel(self, cid: int):
        self.ex("DELETE FROM channels WHERE id=?", (cid,))

    # --- сообщения ---
    def add_message(self, cid: int, user_id: int, content: str) -> int:
        return self.ex(
            "INSERT INTO messages(channel_id, user_id, content, created) VALUES(?,?,?,?)",
            (cid, user_id, content, time.time()),
        )

    def message(self, mid: int):
        return self.q1("SELECT * FROM messages WHERE id=?", (mid,))

    def history(self, cid: int, before: int | None, limit: int):
        if before:
            rows = self.q(
                "SELECT * FROM messages WHERE channel_id=? AND id<? ORDER BY id DESC LIMIT ?", (cid, before, limit)
            )
        else:
            rows = self.q("SELECT * FROM messages WHERE channel_id=? ORDER BY id DESC LIMIT ?", (cid, limit))
        return list(reversed(rows))

    def edit_message(self, mid: int, content: str):
        self.ex("UPDATE messages SET content=?, edited=? WHERE id=?", (content, time.time(), mid))

    def delete_message(self, mid: int) -> list[str]:
        """Удаляет сообщение, возвращает id файлов, которые теперь можно стереть с диска."""
        with self.lock:
            fids = [r[0] for r in self.q("SELECT id FROM files WHERE message_id=?", (mid,))]
            self.ex("DELETE FROM files WHERE message_id=?", (mid,))
            self.ex("DELETE FROM messages WHERE id=?", (mid,))
        return fids

    # --- файлы ---
    def add_file(self, fid: str, user_id: int, name: str, size: int, mime: str):
        self.ex(
            "INSERT INTO files(id, user_id, name, size, mime, created) VALUES(?,?,?,?,?,?)",
            (fid, user_id, name, size, mime, time.time()),
        )

    def file(self, fid: str):
        return self.q1("SELECT * FROM files WHERE id=?", (fid,))

    def attach_files(self, mid: int, user_id: int, fids: list[str]) -> list:
        with self.lock:
            for fid in fids:
                self.ex(
                    "UPDATE files SET message_id=? WHERE id=? AND user_id=? AND message_id IS NULL",
                    (mid, fid, user_id),
                )
            return self.files_of(mid)

    def files_of(self, mid: int):
        return self.q("SELECT id, name, size, mime FROM files WHERE message_id=? ORDER BY created", (mid,))

    def files_of_many(self, mids: list[int]) -> dict[int, list]:
        if not mids:
            return {}
        marks = ",".join("?" * len(mids))
        out: dict[int, list] = {}
        for r in self.q(f"SELECT id, name, size, mime, message_id FROM files WHERE message_id IN ({marks}) ORDER BY created", mids):
            out.setdefault(r["message_id"], []).append(r)
        return out

    def orphan_files(self, older_than: float) -> list[str]:
        """Загруженные, но так и не отправленные файлы."""
        rows = self.q("SELECT id FROM files WHERE message_id IS NULL AND created<?", (older_than,))
        return [r[0] for r in rows]

    def drop_file_rows(self, fids: list[str]):
        with self.lock:
            for fid in fids:
                self.ex("DELETE FROM files WHERE id=?", (fid,))

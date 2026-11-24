"""Консольное управление, когда в админку не попасть.

    python -m beseda_server.manage users
    python -m beseda_server.manage make-admin ЛОГИН
    python -m beseda_server.manage reset-password ЛОГИН [НОВЫЙ_ПАРОЛЬ]
    python -m beseda_server.manage unban ЛОГИН
    python -m beseda_server.manage backup        — архив базы и файлов в ПАПКА_ДАННЫХ/backups

На Ubuntu запускать от имени службы, чтобы прочитать её настройки:
    sudo -u beseda bash -c 'set -a; . /etc/beseda.env; /opt/beseda/venv/bin/python -m beseda_server.manage users'
Работающий сервер увидит изменения сразу (кроме уже открытых сеансов при сбросе пароля — их выкинет при переподключении).
"""
import secrets
import sys

from .config import Config
from .db import DB


def backup(cfg: Config) -> str:
    """Согласованная копия базы (через sqlite backup API, можно на работающем сервере) + файлы, одним .tar.gz."""
    import sqlite3
    import tarfile
    import tempfile
    import time
    out_dir = cfg.data_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"beseda-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        snap = f"{tmp}/beseda.db"
        src = sqlite3.connect(str(cfg.db_path))
        dst = sqlite3.connect(snap)
        src.backup(dst)
        dst.close()
        src.close()
        with tarfile.open(out, "w:gz") as tar:
            tar.add(snap, arcname="beseda.db")
            if cfg.files_dir.exists():
                tar.add(str(cfg.files_dir), arcname="files")
    return str(out)


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cfg = Config()
    db = DB(cfg.db_path)
    cmd, args = argv[0], argv[1:]
    if cmd == "backup":
        print(backup(cfg))
        return 0
    if cmd == "users":
        for u in db.admin_users():
            flags = ("админ " if u["is_admin"] else "") + ("ЗАБЛОКИРОВАН" if u["banned"] else "")
            print(f'{u["id"]:>4}  {u["username"]:<24} {u["display_name"]:<24} {flags}')
        return 0
    if not args:
        print("Укажите логин")
        return 1
    u = db.user_by_name(args[0])
    if not u:
        print(f"Нет пользователя {args[0]}")
        return 1
    if cmd == "make-admin":
        db.set_admin(u["id"], True)
        print(f'{u["username"]} теперь администратор')
    elif cmd == "reset-password":
        pw = args[1] if len(args) > 1 else secrets.token_urlsafe(9)
        db.set_password(u["id"], pw)
        db.delete_sessions_of(u["id"])
        print(f'Новый пароль для {u["username"]}: {pw}')
    elif cmd == "unban":
        db.set_banned(u["id"], False)
        print(f'{u["username"]} разблокирован')
    else:
        print(f"Неизвестная команда {cmd}\n{__doc__}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

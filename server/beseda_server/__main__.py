"""Запуск: python -m beseda_server"""
import logging
import socket

import uvicorn

from .app import create_app
from .config import Config


def local_ips() -> list[str]:
    ips = set()
    try:  # адрес, через который машина ходит в сеть (пакет никуда не отправляется)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def banner(cfg: Config):
    print("=" * 60)
    print(" Беседа — сервер запущен")
    if cfg.public_url:
        print(f"   адрес для клиентов:   {cfg.public_url}")
        print(f"   админка:              {cfg.public_url.rstrip('/')}/admin")
    elif cfg.host in ("0.0.0.0", ""):
        print(f"   на этом компьютере:   http://127.0.0.1:{cfg.port}")
        for ip in local_ips():
            print(f"   из локальной сети:    http://{ip}:{cfg.port}")
    else:
        print(f"   адрес: http://{cfg.host}:{cfg.port}")
    print(f"   голос: UDP {cfg.udp_port}")
    print(f"   код регистрации: {cfg.registration_code or 'не нужен'}")
    print(f"   данные: {cfg.data_dir}")
    print(" Остановить: Ctrl+C")
    print("=" * 60, flush=True)


def apply_pending_restore(cfg: Config):
    """Если админка подготовила восстановление из копии (папка data/_restore) — применяем
    ДО открытия базы. Прежние данные не пропадают: уезжают в backups/pre-restore-<время>."""
    import shutil
    import time
    stage = cfg.data_dir / "_restore"
    if not (stage / "beseda.db").exists():
        return
    safe = cfg.data_dir / "backups" / f"pre-restore-{time.strftime('%Y%m%d-%H%M%S')}"
    safe.mkdir(parents=True, exist_ok=True)
    for p in cfg.data_dir.glob("beseda.db*"):
        shutil.move(str(p), safe / p.name)
    shutil.move(str(stage / "beseda.db"), cfg.db_path)
    if (stage / "files").exists():
        if cfg.files_dir.exists():
            shutil.move(str(cfg.files_dir), safe / "files")
        shutil.move(str(stage / "files"), cfg.files_dir)
    shutil.rmtree(stage, ignore_errors=True)
    print(f"Восстановление из резервной копии выполнено; прежние данные — в {safe}", flush=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    apply_pending_restore(cfg)
    app = create_app(cfg)
    banner(cfg)
    uvicorn.run(
        app,
        host=cfg.host,
        port=cfg.port,
        proxy_headers=True,
        forwarded_allow_ips=cfg.trusted_proxies,
        ws_ping_interval=20,
        ws_ping_timeout=20,
        ws_max_size=1024 * 1024,
        log_level="info",
    )


if __name__ == "__main__":
    main()

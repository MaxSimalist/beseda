"""Настройки сервера. Всё берётся из переменных окружения (см. deploy/beseda.env)."""
import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(_env("BESEDA_DATA", "./data")).resolve())
    # HTTP/WebSocket — слушаем только localhost, наружу смотрит nginx с TLS
    host: str = field(default_factory=lambda: _env("BESEDA_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("BESEDA_PORT", "8765")))
    # UDP для голоса — смотрит прямо в интернет
    udp_host: str = field(default_factory=lambda: _env("BESEDA_UDP_HOST", "0.0.0.0"))
    udp_port: int = field(default_factory=lambda: int(_env("BESEDA_UDP_PORT", "50000")))
    # Если клиенты должны слать голос на другой адрес, чем адрес сайта
    udp_public_host: str = field(default_factory=lambda: _env("BESEDA_UDP_PUBLIC_HOST", ""))
    # Код приглашения для регистрации. Пусто = регистрация открыта для всех
    registration_code: str = field(default_factory=lambda: _env("BESEDA_REG_CODE", ""))
    max_upload_mb: int = field(default_factory=lambda: int(_env("BESEDA_MAX_UPLOAD_MB", "50")))
    # Чьим заголовкам X-Forwarded-For верить (адрес обратного прокси). В Docker за Caddy — "*",
    # потому что порт сервера наружу не открыт и обратиться к нему может только Caddy.
    trusted_proxies: str = field(default_factory=lambda: _env("BESEDA_TRUSTED_PROXIES", "127.0.0.1"))
    # Адрес для клиентов — только для сообщения при запуске
    public_url: str = field(default_factory=lambda: _env("BESEDA_PUBLIC_URL", ""))

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "beseda.db"

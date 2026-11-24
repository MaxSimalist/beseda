"""REST-клиент и запуск блокирующих задач в фоне (QThreadPool)."""
import mimetypes
import os
import ssl
from urllib.parse import urlparse

import requests
import urllib3
from requests.adapters import HTTPAdapter
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class InsecureAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        kwargs["assert_hostname"] = False
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self.poolmanager.connection_pool_kw.get("ssl_context")
        kwargs["assert_hostname"] = False
        return super().proxy_manager_for(*args, **kwargs)


class ApiError(Exception):
    pass


class Api:
    def __init__(self, base_url: str, token: str | None = None, insecure_ssl: bool = False):
        url = base_url.strip().rstrip("/")
        if "://" not in url:
            url = "https://" + url
        self.base = url
        self.token = token
        self.insecure_ssl = insecure_ssl
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "Beseda-Client/1.0"
        self.s.verify = not insecure_ssl
        if insecure_ssl:
            adapter = InsecureAdapter()
            self.s.mount("https://", adapter)
            self.s.mount("http://", adapter)

    @property
    def host(self) -> str:
        return urlparse(self.base).hostname or "localhost"

    @property
    def ws_url(self) -> str:
        u = urlparse(self.base)
        scheme = "wss" if u.scheme == "https" else "ws"
        return f"{scheme}://{u.netloc}{u.path}/ws"

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    @staticmethod
    def _check(r: requests.Response) -> dict:
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            raise ApiError(data.get("error") or f"Ошибка сервера: HTTP {r.status_code}")
        return data

    def info(self) -> dict:
        try:
            return self._check(self.s.get(self.base + "/api/info", timeout=10))
        except requests.RequestException as e:
            raise ApiError(f"Сервер недоступен: {e.__class__.__name__}") from e

    def login(self, username: str, password: str) -> dict:
        try:
            d = self._check(self.s.post(self.base + "/api/login", json={"username": username, "password": password}, timeout=15))
        except requests.RequestException as e:
            raise ApiError(f"Сервер недоступен: {e.__class__.__name__}") from e
        self.token = d["token"]
        return d

    def register(self, username: str, password: str, code: str) -> dict:
        try:
            d = self._check(self.s.post(self.base + "/api/register", json={"username": username, "password": password, "code": code}, timeout=15))
        except requests.RequestException as e:
            raise ApiError(f"Сервер недоступен: {e.__class__.__name__}") from e
        self.token = d["token"]
        return d

    def logout(self):
        try:
            self.s.post(self.base + "/api/logout", headers=self._h(), timeout=5)
        except requests.RequestException:
            pass

    def change_password(self, old: str, new: str):
        self._check(self.s.post(self.base + "/api/password", json={"old": old, "new": new}, headers=self._h(), timeout=15))

    def upload(self, path: str, progress=None) -> dict:
        size = os.path.getsize(path)
        name = os.path.basename(path)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"

        class Reader:
            def __init__(self, f):
                self.f, self.done = f, 0

            def read(self, n=-1):
                chunk = self.f.read(n if n and n > 0 else 256 * 1024)
                self.done += len(chunk)
                if progress:
                    progress(self.done, size)
                return chunk

        with open(path, "rb") as f:
            r = self.s.post(self.base + "/api/upload", headers=self._h(), files={"file": (name, Reader(f), mime)}, timeout=(10, 600))
        return self._check(r)

    def fetch(self, fid: str) -> bytes:
        r = self.s.get(f"{self.base}/api/files/{fid}", headers=self._h(), timeout=60)
        if r.status_code >= 400:
            self._check(r)
        return r.content

    def download(self, fid: str, dest: str, progress=None):
        with self.s.get(f"{self.base}/api/files/{fid}", headers=self._h(), stream=True, timeout=(10, 600)) as r:
            if r.status_code >= 400:
                self._check(r)
            total = int(r.headers.get("content-length") or 0)
            done = 0
            tmp = dest + ".part"
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(256 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            os.replace(tmp, dest)
        return dest


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)


class _Task(QRunnable):
    def __init__(self, fn, args, kwargs, with_progress: bool):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.sig = _Signals()
        if with_progress:
            self.kwargs["progress"] = lambda a, b: self.sig.progress.emit(int(a), int(b))
        self.setAutoDelete(False)

    def run(self):
        try:
            res = self.fn(*self.args, **self.kwargs)
        except ApiError as e:
            self.sig.failed.emit(str(e))
        except requests.RequestException as e:
            self.sig.failed.emit(f"Сеть: {e.__class__.__name__}")
        except Exception as e:
            self.sig.failed.emit(f"{e.__class__.__name__}: {e}")
        else:
            self.sig.done.emit(res)

_alive = set()

def run_bg(fn, *args, on_done=None, on_fail=None, on_progress=None, **kwargs):
    task = _Task(fn, args, kwargs, on_progress is not None)
    _alive.add(task)
    def finish(*_):
        _alive.discard(task)
    if on_done: task.sig.done.connect(on_done)
    if on_fail: task.sig.failed.connect(on_fail)
    if on_progress: task.sig.progress.connect(on_progress)
    task.sig.done.connect(finish)
    task.sig.failed.connect(finish)
    QThreadPool.globalInstance().start(task)
    return task

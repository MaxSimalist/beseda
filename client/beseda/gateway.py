"""WebSocket-соединение с сервером."""
import itertools
import json
import logging
from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkRequest, QSslConfiguration, QSslSocket
from PySide6.QtWebSockets import QWebSocket

log = logging.getLogger("beseda.gateway")

class Gateway(QObject):
    event = Signal(dict)
    online = Signal()
    offline = Signal(str)
    auth_failed = Signal(str)

    def __init__(self, url: str, token: str, parent=None, insecure_ssl: bool = False):
        super().__init__(parent)
        self.url = url
        self.token = token
        self.insecure_ssl = insecure_ssl
        self.ws = QWebSocket()
        self.ws.connected.connect(self._on_connected)
        self.ws.disconnected.connect(self._on_disconnected)
        self.ws.textMessageReceived.connect(self._on_text)
        if insecure_ssl:
            self.ws.sslErrors.connect(self._on_ssl_errors)
        self._req = itertools.count(1)
        self._pending = {}
        self._backoff = 1
        self._stopped = False
        self._is_online = False
        self._reconnect = QTimer(self)
        self._reconnect.setSingleShot(True)
        self._reconnect.timeout.connect(self._open)
        self._ping = QTimer(self)
        self._ping.setInterval(25000)
        self._ping.timeout.connect(lambda: self.send({"type": "ping"}))

    def _on_ssl_errors(self, errors):
        for e in errors:
            log.warning("SSL error: %s", e.errorString())
        self.ws.ignoreSslErrors()

    @property
    def is_online(self) -> bool:
        return self._is_online

    def start(self):
        self._stopped = False
        self._open()

    def stop(self):
        self._stopped = True
        self._reconnect.stop()
        self._ping.stop()
        self.ws.close()

    def _open(self):
        if self._stopped:
            return
        req = QNetworkRequest(QUrl(self.url))
        if self.insecure_ssl:
            conf = QSslConfiguration.defaultConfiguration()
            conf.setPeerVerifyMode(QSslSocket.VerifyNone)
            req.setSslConfiguration(conf)
        self.ws.open(req)

    def _on_connected(self):
        self.ws.sendTextMessage(json.dumps({"type": "auth", "token": self.token}))

    def _on_disconnected(self):
        was = self._is_online
        self._is_online = False
        self._ping.stop()
        self._pending.clear()
        if self._stopped:
            return
        reason = self.ws.errorString() or "соединение закрыто"
        if was:
            self.offline.emit(reason)
        self._reconnect.start(self._backoff * 1000)
        self._backoff = min(self._backoff * 2, 30)

    def _on_text(self, text: str):
        try:
            msg = json.loads(text)
        except ValueError:
            return
        t = msg.get("type")
        if t == "auth_failed":
            self._stopped = True
            self.auth_failed.emit(str(msg.get("reason") or ""))
            return
        if t == "ready":
            self._is_online = True
            self._backoff = 1
            self._ping.start()
            self.online.emit()
        req = msg.get("req")
        if req is not None and req in self._pending:
            cb = self._pending.pop(req)
            try:
                cb(msg)
            except Exception:
                pass
        self.event.emit(msg)

    def send(self, payload: dict, callback=None) -> bool:
        if not self._is_online:
            return False
        if callback is not None:
            rid = next(self._req)
            payload = dict(payload, req=rid)
            self._pending[rid] = callback
        self.ws.sendTextMessage(json.dumps(payload, ensure_ascii=False))
        return True

"""Панель просмотра демонстрации экрана: декодирование H.264 в своём потоке,
показ QImage в QLabel с сохранением пропорций."""
import logging
import queue
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .. import screen

log = logging.getLogger("beseda.video")


class VideoPanel(QWidget):
    """Показывает одну трансляцию. Кадры подаются через feed() из любого потока."""

    closed = Signal(int)   # uid трансляции, которую закрыли

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("videoPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("#videoPanel { background: #000; border-radius: 8px; }"
                           "#videoPanel QLabel { color: #b5bac1; }")
        self.uid: int | None = None
        self.title = QLabel("")
        self.title.setStyleSheet("padding: 4px 8px; font-weight: 600;")
        self.frame_label = QLabel("Ожидание видео…")
        self.frame_label.setAlignment(Qt.AlignCenter)
        self.frame_label.setMinimumHeight(200)
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(28, 28)
        close_btn.setToolTip("Закрыть просмотр")
        close_btn.setStyleSheet("QPushButton { background: transparent; color: #b5bac1; border: none;"
                                "font-size: 12pt; } QPushButton:hover { color: #fff; }")
        close_btn.clicked.connect(self.stop)
        head = QHBoxLayout()
        head.setContentsMargins(8, 4, 6, 0)
        head.addWidget(self.title, 1)
        head.addWidget(close_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 6)
        lay.setSpacing(2)
        lay.addLayout(head)
        lay.addWidget(self.frame_label, 1)
        self.setMinimumHeight(260)

        self._q: queue.Queue = queue.Queue(maxsize=4)
        self._decoder = screen.VideoDecoder()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._decode_loop, name="video-decode", daemon=True)
        self._thread.start()

    # --- из потока приёма (voice engine) ---
    def feed(self, uid: int, data: bytes, key: bool):
        if self.uid is not None and uid != self.uid:
            return
        try:
            self._q.put_nowait((data, key))
        except queue.Full:
            try:                                    # зритель не успевает — выбрасываем старый кадр
                self._q.get_nowait()
                self._q.put_nowait((data, key))
            except Exception:
                pass

    def set_source(self, uid: int, name: str):
        self.uid = uid
        self.title.setText(f"🖥  {name} — демонстрация экрана")

    def _decode_loop(self):
        while not self._stop.is_set():
            try:
                data, key = self._q.get(timeout=0.3)
            except queue.Empty:
                continue
            img = self._decoder.decode(data)
            if img is not None and not img.isNull():
                self._show(img)

    def _show(self, img):
        try:
            pm = QPixmap.fromImage(img)
            if pm.isNull():
                return
            self.frame_label.setPixmap(pm.scaled(self.frame_label.size(),
                                                 Qt.KeepAspectRatio, Qt.SmoothTransformation))
        except RuntimeError:
            pass                                    # виджет уже удалён

    def resizeEvent(self, e):
        super().resizeEvent(e)
        pm = self.frame_label.pixmap()
        if pm and not pm.isNull():
            self.frame_label.setPixmap(pm.scaled(self.frame_label.size(),
                                                 Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def stop(self):
        old, self.uid = self.uid, None
        self._stop.set()
        self.closed.emit(old or 0)
        self.hide()

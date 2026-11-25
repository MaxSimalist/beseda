"""Поиск GIF через GIPHY: диалог с сеткой результатов.

Нужен бесплатный API-ключ GIPHY (https://developers.giphy.com → Create an App,
тип API). Ключ хранится в настройках клиента (giphy_key).
"""
import logging
import os
import tempfile
import time

import requests
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from ..api import run_bg

log = logging.getLogger("beseda.gif")

API = "https://api.giphy.com/v1/gifs"


def search(api_key: str, query: str, limit: int = 24) -> list[dict]:
    r = requests.get(f"{API}/search", params={"api_key": api_key, "q": query, "limit": limit,
                                              "rating": "pg-13", "lang": "ru"}, timeout=10)
    r.raise_for_status()
    out = []
    for g in r.json().get("data", []):
        imgs = g.get("images", {})
        preview = (imgs.get("fixed_width_downsampled") or imgs.get("fixed_width") or {}).get("url")
        full = (imgs.get("downsized") or imgs.get("original") or {}).get("url")
        if preview and full:
            out.append({"preview": preview, "full": full, "title": g.get("title") or "gif"})
    return out


def trending(api_key: str, limit: int = 24) -> list[dict]:
    r = requests.get(f"{API}/trending", params={"api_key": api_key, "limit": limit, "rating": "pg-13"},
                     timeout=10)
    r.raise_for_status()
    out = []
    for g in r.json().get("data", []):
        imgs = g.get("images", {})
        preview = (imgs.get("fixed_width_downsampled") or imgs.get("fixed_width") or {}).get("url")
        full = (imgs.get("downsized") or imgs.get("original") or {}).get("url")
        if preview and full:
            out.append({"preview": preview, "full": full, "title": g.get("title") or "gif"})
    return out


def download_gif(url: str) -> str:
    """Скачать GIF во временный файл, вернуть путь."""
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    p = os.path.join(tempfile.gettempdir(), f"gif_{int(time.time() * 1000)}.gif")
    with open(p, "wb") as f:
        f.write(r.content)
    return p


class _Cell(QLabel):
    clicked = Signal(dict)

    def __init__(self, item: dict):
        super().__init__("…")
        self.item = item
        self.setFixedSize(200, 150)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(item["title"])
        self.setStyleSheet("QLabel { background: #111214; border-radius: 6px; color: #6d717a; }"
                           "QLabel:hover { border: 2px solid #5865f2; }")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.item)
        super().mousePressEvent(e)


class GifDialog(QDialog):
    """Поиск и выбор GIF. selected — URL выбранной гифки (original/downsized)."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.selected: str | None = None
        self.setWindowTitle("Гифки — GIPHY")
        self.resize(660, 520)
        lay = QVBoxLayout(self)
        self.key_row = QHBoxLayout()
        self.key_edit = QLineEdit(self.s.value("giphy_key", ""))
        self.key_edit.setPlaceholderText("API-ключ GIPHY (developers.giphy.com → Create an App → API)")
        save_key = QPushButton("Сохранить ключ")
        save_key.clicked.connect(self._save_key)
        self.key_row.addWidget(QLabel("Ключ GIPHY:"))
        self.key_row.addWidget(self.key_edit, 1)
        self.key_row.addWidget(save_key)
        lay.addLayout(self.key_row)

        row = QHBoxLayout()
        self.q = QLineEdit()
        self.q.setPlaceholderText("Поиск гифок… (Enter)")
        self.q.returnPressed.connect(self._search)
        btn = QPushButton("Найти")
        btn.clicked.connect(self._search)
        row.addWidget(self.q, 1)
        row.addWidget(btn)
        lay.addLayout(row)

        self.status = QLabel("")
        self.status.setObjectName("muted")
        lay.addWidget(self.status)
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(8)
        self.area.setWidget(self.grid_host)
        lay.addWidget(self.area, 1)
        self._cells: list[_Cell] = []
        if self.s.value("giphy_key", ""):
            self.key_row.setVisible(False)
            self._load(lambda: trending(self.s.value("giphy_key", "")))
        else:
            self.status.setText("Вставьте ключ GIPHY — он бесплатный: developers.giphy.com → "
                                "Create an App → выберите «API» → скопируйте ключ.")

    def _save_key(self):
        key = self.key_edit.text().strip()
        self.s.setValue("giphy_key", key)
        if key:
            self.key_row.setVisible(False)
            self._load(lambda: trending(key))
        else:
            self.status.setText("Ключ пустой.")

    def _search(self):
        key = self.s.value("giphy_key", "")
        q = self.q.text().strip()
        if not key:
            self.key_row.setVisible(True)
            return
        if q:
            self._load(lambda: search(key, q))

    def _load(self, fn):
        self.status.setText("Загрузка…")
        for c in self._cells:
            c.deleteLater()
        self._cells = []
        run_bg(fn, on_done=self._filled, on_fail=lambda e: self.status.setText(f"Ошибка: {e}"))

    def _filled(self, items: list[dict]):
        self.status.setText("" if items else "Ничего не найдено.")
        for i, item in enumerate(items):
            cell = _Cell(item)
            cell.clicked.connect(self._choose)
            self.grid.addWidget(cell, i // 3, i % 3)
            self._cells.append(cell)

            def show(img, cell=cell):
                if isinstance(img, QImage) and not img.isNull() and cell in self._cells:
                    cell.setPixmap(QPixmap.fromImage(img).scaled(
                        196, 146, Qt.KeepAspectRatio, Qt.SmoothTransformation))

            run_bg(lambda u=item["preview"]: _fetch_image(u), on_done=show, on_fail=lambda e: None)

    def _choose(self, item: dict):
        self.selected = item["full"]
        self.accept()


def _fetch_image(url: str) -> QImage:
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    img = QImage()
    img.loadFromData(r.content)
    return img

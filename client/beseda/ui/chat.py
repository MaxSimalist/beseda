"""Область чата: лента сообщений, вложения, поле ввода."""
import os
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImage, QPixmap
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QMenu, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ..api import run_bg
from .style import avatar_pixmap, fmt_size, fmt_time, name_color, render_text

GROUP_GAP = 7 * 60  # сообщения одного автора в пределах 7 минут склеиваются
THUMB_W, THUMB_H = 420, 320


def cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(Path.home(), ".cache")
    d = Path(base) / "Beseda" / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- картинки
class ImageCache(QObject):
    """Загружает картинки вложений в фоне, кэширует в памяти и на диске."""

    def __init__(self, api, parent=None):
        super().__init__(parent)
        self.api = api
        self.mem: dict[str, QImage] = {}
        self.waiting: dict[str, list] = {}

    def path(self, fid: str, name: str = "") -> Path:
        ext = os.path.splitext(name)[1][:8]
        return cache_dir() / f"{fid}{ext}"

    def get(self, fid: str, name: str, callback):
        if fid in self.mem:
            callback(self.mem[fid])
            return
        if fid in self.waiting:
            self.waiting[fid].append(callback)
            return
        self.waiting[fid] = [callback]
        p = self.path(fid, name)

        def load():
            if p.exists():
                data = p.read_bytes()
            else:
                data = self.api.fetch(fid)
                p.write_bytes(data)
            img = QImage()
            img.loadFromData(data)
            return img

        def done(img):
            if not img.isNull():
                if img.width() > THUMB_W or img.height() > THUMB_H:
                    img = img.scaled(THUMB_W, THUMB_H, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.mem[fid] = img
            for cb in self.waiting.pop(fid, []):
                try:
                    cb(img)
                except RuntimeError:
                    pass  # виджет уже удалён

        run_bg(load, on_done=done, on_fail=lambda e: self.waiting.pop(fid, None))


class ClickLabel(QLabel):
    clicked = Signal()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


# ---------------------------------------------------------------- одно сообщение
class MessageWidget(QWidget):
    def __init__(self, msg: dict, header: bool, view: "MessageView"):
        super().__init__()
        self.msg = msg
        self.view = view
        self.setObjectName("message")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 6 if header else 1, 16, 1)
        row.setSpacing(14)
        left = QLabel()
        left.setFixedWidth(40)
        left.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        if header:
            left.setPixmap(avatar_pixmap(msg["author_name"], 40, key=str(msg["author_id"])))
        row.addWidget(left)

        col = QVBoxLayout()
        col.setSpacing(2)
        col.setContentsMargins(0, 0, 0, 0)
        if header:
            h = QLabel(
                f'<span style="color:{name_color(str(msg["author_id"]))};font-weight:600;font-size:10.5pt;">'
                f'{_esc(msg["author_name"])}</span>'
                f'&nbsp;&nbsp;<span style="color:#949ba4;font-size:8.5pt;">{fmt_time(msg["created"])}</span>')
            col.addWidget(h)
        if msg["content"]:
            text = render_text(msg["content"], view.my_name)
            if msg.get("edited"):
                text += ' <span style="color:#949ba4;font-size:8pt;">(изменено)</span>'
            body = QLabel(text)
            body.setWordWrap(True)
            body.setTextFormat(Qt.RichText)
            body.setOpenExternalLinks(True)
            body.setTextInteractionFlags(Qt.TextBrowserInteraction)
            body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            body.setContextMenuPolicy(Qt.NoContextMenu)
            if not header:
                body.setToolTip(fmt_time(msg["created"]))
            col.addWidget(body)
        for att in msg.get("attachments", []):
            col.addWidget(self._attachment(att), 0, Qt.AlignLeft)
        row.addLayout(col, 1)

    def _attachment(self, att: dict) -> QWidget:
        if att["mime"].startswith("image/") and att["size"] < 25 * 1024 * 1024:
            lbl = ClickLabel("Загрузка изображения…")
            lbl.setObjectName("muted")
            lbl.setCursor(Qt.PointingHandCursor)
            lbl.setToolTip(f'{att["name"]} · {fmt_size(att["size"])} — нажмите, чтобы открыть')
            lbl.clicked.connect(lambda a=att: self.view.open_attachment.emit(a))

            def show(img, lbl=lbl):
                if img.isNull():
                    lbl.setText(f'🖼 {att["name"]} (не удалось показать)')
                else:
                    lbl.setPixmap(QPixmap.fromImage(img))
                    lbl.setFixedSize(img.width(), img.height())
                    self.view.content_resized()

            self.view.images.get(att["id"], att["name"], show)
            return lbl
        btn = QPushButton(f'📄  {att["name"]}\n      {fmt_size(att["size"])}')
        btn.setObjectName("fileBtn")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip("Сохранить файл")
        btn.clicked.connect(lambda _=False, a=att: self.view.save_attachment.emit(a))
        return btn

    def _menu(self, pos):
        m = QMenu(self)
        mine = self.msg["author_id"] == self.view.my_id
        if self.msg["content"]:
            m.addAction("Копировать текст", lambda: self.view.action.emit("copy", self.msg))
        m.addAction("Ответить с упоминанием", lambda: self.view.action.emit("mention", self.msg))
        if mine:
            m.addAction("Редактировать", lambda: self.view.action.emit("edit", self.msg))
        if mine or self.view.can_moderate:
            m.addSeparator()
            m.addAction("Удалить сообщение", lambda: self.view.action.emit("delete", self.msg))
        m.exec(self.mapToGlobal(pos))


def _esc(s: str) -> str:
    import html
    return html.escape(s)


# ---------------------------------------------------------------- лента
class MessageView(QScrollArea):
    load_more = Signal()
    action = Signal(str, dict)
    open_attachment = Signal(dict)
    save_attachment = Signal(dict)

    def __init__(self, images: ImageCache):
        super().__init__()
        self.images = images
        self.my_id = 0
        self.my_name = ""
        self.can_moderate = False
        self.has_more = False
        self.loading = False
        self.messages: list[dict] = []
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.inner = QWidget()
        self.lay = QVBoxLayout(self.inner)
        self.lay.setContentsMargins(0, 8, 0, 16)
        self.lay.setSpacing(0)
        self.lay.addStretch(1)
        self.more_btn = QPushButton("Загрузить более ранние сообщения")
        self.more_btn.setObjectName("loadMore")
        self.more_btn.clicked.connect(self._want_more)
        self.lay.insertWidget(0, self.more_btn)
        self.more_btn.hide()
        self.setWidget(self.inner)
        self._stick = True
        self._keep_from_bottom: int | None = None
        sb = self.verticalScrollBar()
        sb.valueChanged.connect(self._scrolled)
        sb.rangeChanged.connect(self._range_changed)

    # --- прокрутка ---
    def _scrolled(self, v):
        sb = self.verticalScrollBar()
        self._stick = v >= sb.maximum() - 40
        if v <= sb.minimum() + 5 and self.has_more and not self.loading and self.messages:
            self._want_more()

    def _want_more(self):
        if self.has_more and not self.loading:
            self.loading = True
            self.more_btn.setText("Загрузка…")
            self.load_more.emit()

    def _range_changed(self, lo, hi):
        sb = self.verticalScrollBar()
        if self._keep_from_bottom is not None:
            sb.setValue(hi - self._keep_from_bottom)
            self._keep_from_bottom = None
        elif self._stick:
            sb.setValue(hi)

    def content_resized(self):
        if self._stick:
            QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))

    def scroll_to_bottom(self):
        self._stick = True
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))

    # --- содержимое ---
    def _clear(self):
        while self.lay.count() > 2:  # кнопка «ещё» + stretch остаются
            item = self.lay.takeAt(1)
            w = item.widget()
            if w:
                w.deleteLater()

    def set_messages(self, messages: list[dict], has_more: bool, prepend: bool = False):
        sb = self.verticalScrollBar()
        if prepend:
            self._keep_from_bottom = sb.maximum() - sb.value()
        self.messages = list(messages)
        self.has_more = has_more
        self.loading = False
        self.more_btn.setText("Загрузить более ранние сообщения")
        self.more_btn.setVisible(has_more)
        self.setUpdatesEnabled(False)
        self._clear()
        prev = None
        for m in self.messages:
            self.lay.insertWidget(self.lay.count() - 1, MessageWidget(m, self._needs_header(prev, m), self))
            prev = m
        self.setUpdatesEnabled(True)
        if not prepend:
            self.scroll_to_bottom()

    def append(self, m: dict):
        prev = self.messages[-1] if self.messages else None
        self.messages.append(m)
        self.lay.insertWidget(self.lay.count() - 1, MessageWidget(m, self._needs_header(prev, m), self))
        if m["author_id"] == self.my_id:
            self.scroll_to_bottom()

    @staticmethod
    def _needs_header(prev, m) -> bool:
        return not (prev and prev["author_id"] == m["author_id"] and m["created"] - prev["created"] < GROUP_GAP)


# ---------------------------------------------------------------- поле ввода
class InputEdit(QPlainTextEdit):
    submit = Signal()
    paste_image = Signal(QImage)
    paste_files = Signal(list)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not (e.modifiers() & Qt.ShiftModifier):
            self.submit.emit()
            return
        super().keyPressEvent(e)

    def insertFromMimeData(self, source):
        if source.hasImage():
            img = source.imageData()
            if isinstance(img, QImage) and not img.isNull():
                self.paste_image.emit(img)
                return
        if source.hasUrls() and source.urls() and all(u.isLocalFile() for u in source.urls()):
            self.paste_files.emit([u.toLocalFile() for u in source.urls()])
            return
        super().insertFromMimeData(source)

    def sizeHint(self):
        return QSize(100, 44)


class Composer(QWidget):
    send = Signal(str, list)   # текст, пути к файлам
    typing = Signal()

    def __init__(self):
        super().__init__()
        self.files: list[str] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 0, 16, 16)
        lay.setSpacing(6)
        self.chips = QHBoxLayout()
        self.chips.setSpacing(6)
        self.chips.addStretch(1)
        lay.addLayout(self.chips)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.hide()
        lay.addWidget(self.status)

        row = QHBoxLayout()
        row.setSpacing(0)
        self.attach = QPushButton("+")
        self.attach.setObjectName("attach")
        self.attach.setFixedSize(44, 44)
        self.attach.setToolTip("Прикрепить файл (можно перетащить или вставить Ctrl+V)")
        self.attach.clicked.connect(self._pick)
        self.edit = InputEdit()
        self.edit.setObjectName("input")
        self.edit.setFixedHeight(44)
        self.edit.setPlaceholderText("Написать сообщение…")
        self.edit.submit.connect(self._submit)
        self.edit.paste_image.connect(self._paste_image)
        self.edit.paste_files.connect(self.add_files)
        self.edit.textChanged.connect(self._changed)
        row.addWidget(self.attach)
        row.addSpacing(6)
        row.addWidget(self.edit, 1)
        lay.addLayout(row)
        self._last_typing = 0.0

    def set_channel_name(self, name: str):
        self.edit.setPlaceholderText(f"Написать в #{name}")

    def _changed(self):
        doc_h = int(self.edit.document().size().height() * self.edit.fontMetrics().lineSpacing()) + 20
        self.edit.setFixedHeight(max(44, min(doc_h, 200)))
        if self.edit.toPlainText().strip() and time.time() - self._last_typing > 4:
            self._last_typing = time.time()
            self.typing.emit()

    def _pick(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Прикрепить файлы")
        self.add_files(paths)

    def _paste_image(self, img: QImage):
        p = os.path.join(tempfile.gettempdir(), f"скриншот_{time.strftime('%Y%m%d_%H%M%S')}.png")
        img.save(p, "PNG")
        self.add_files([p])

    def add_files(self, paths: list[str]):
        for p in paths:
            if p and os.path.isfile(p) and p not in self.files and len(self.files) < 10:
                self.files.append(p)
        self._render_chips()
        self.edit.setFocus()

    def _render_chips(self):
        while self.chips.count() > 1:
            w = self.chips.takeAt(0).widget()
            if w:
                w.deleteLater()
        for p in self.files:
            b = QPushButton(f"📎 {os.path.basename(p)}  ✕")
            b.setObjectName("chip")
            b.setToolTip("Убрать вложение")
            b.clicked.connect(lambda _=False, p=p: self._remove(p))
            self.chips.insertWidget(self.chips.count() - 1, b)

    def _remove(self, p):
        if p in self.files:
            self.files.remove(p)
        self._render_chips()

    def _submit(self):
        text = self.edit.toPlainText().strip()
        if not text and not self.files:
            return
        self.send.emit(text, list(self.files))
        self.edit.clear()
        self.files.clear()
        self._render_chips()

    def insert_text(self, s: str):
        self.edit.insertPlainText(s)
        self.edit.setFocus()

    def set_status(self, text: str):
        self.status.setText(text)
        self.status.setVisible(bool(text))


# ---------------------------------------------------------------- панель чата целиком
class ChatPanel(QWidget):
    def __init__(self, images: ImageCache):
        super().__init__()
        self.setObjectName("chatPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAcceptDrops(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QWidget()
        bar.setObjectName("headerBar")
        bar.setAttribute(Qt.WA_StyledBackground, True)
        bar.setFixedHeight(48)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(4, 0, 8, 0)
        self.title = QLabel("")
        self.title.setObjectName("header")
        bl.addWidget(self.title)
        bl.addStretch(1)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        bl.addWidget(self.status)
        lay.addWidget(bar)
        self.view = MessageView(images)
        lay.addWidget(self.view, 1)
        self.typing = QLabel("")
        self.typing.setObjectName("typing")
        self.typing.setFixedHeight(18)
        lay.addWidget(self.typing)
        self.composer = Composer()
        lay.addWidget(self.composer)
        self.placeholder = QLabel("Выберите текстовый канал слева")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setObjectName("muted")
        lay.addWidget(self.placeholder, 1)
        self.set_active(False)

    def set_active(self, on: bool):
        self.view.setVisible(on)
        self.composer.setVisible(on)
        self.typing.setVisible(on)
        self.placeholder.setVisible(not on)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and self.composer.isVisible():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.composer.add_files([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])


def open_local(path: str):
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))

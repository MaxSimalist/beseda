"""Тёмная тема в духе Discord и мелкие помощники для интерфейса."""
import hashlib
import html
import re
import time

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap

BG0 = "#1e1f22"   # полоса серверов
BG1 = "#2b2d31"   # панель каналов / участников
BG2 = "#313338"   # чат
BG3 = "#383a40"   # поле ввода
BG_HOVER = "#35373c"
TEXT = "#dbdee1"
MUTED = "#949ba4"
ACCENT = "#5865f2"
GREEN = "#23a55a"
RED = "#da373c"

QSS = f"""
* {{ font-family: "Segoe UI", "Noto Sans", sans-serif; font-size: 10pt; color: {TEXT}; }}
QMainWindow, QDialog {{ background: {BG2}; }}
QWidget#guildBar {{ background: {BG0}; }}
QWidget#sidePanel, QWidget#memberPanel {{ background: {BG1}; }}
QWidget#chatPanel {{ background: {BG2}; }}
QWidget#userPanel {{ background: #232428; }}
QWidget#voicePanel {{ background: #232428; border-bottom: 1px solid {BG1}; }}
QLabel#header {{ font-weight: 600; font-size: 11pt; padding: 0 12px; }}
QWidget#headerBar {{ border-bottom: 1px solid {BG0}; }}
QLabel#muted, QLabel#typing {{ color: {MUTED}; }}
QLabel#typing {{ font-size: 9pt; padding-left: 16px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #1a1b1e; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QTreeWidget, QListWidget {{ background: transparent; border: none; outline: 0; }}
QTreeWidget::item, QListWidget::item {{ padding: 4px 6px; border-radius: 4px; color: {MUTED}; }}
QTreeWidget::item:hover, QListWidget::item:hover {{ background: {BG_HOVER}; color: {TEXT}; }}
QTreeWidget::item:selected, QListWidget::item:selected {{ background: #404249; color: white; }}
QTreeWidget::branch {{ background: transparent; image: none; border: none; }}
QPlainTextEdit#input {{ background: {BG3}; border: none; border-radius: 8px; padding: 8px 10px; }}
QLineEdit, QComboBox, QSpinBox {{ background: {BG0}; border: 1px solid {BG0}; border-radius: 4px; padding: 6px; }}
QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
QComboBox QAbstractItemView {{ background: {BG1}; selection-background-color: {ACCENT}; }}
QPushButton {{ background: {ACCENT}; color: white; border: none; border-radius: 4px; padding: 7px 16px; font-weight: 600; }}
QPushButton:hover {{ background: #4752c4; }}
QPushButton:disabled {{ background: #3c4270; color: #aaa; }}
QPushButton#flat {{ background: transparent; color: {MUTED}; padding: 4px 8px; font-weight: normal; }}
QPushButton#flat:hover {{ background: {BG_HOVER}; color: {TEXT}; }}
QPushButton#danger {{ background: {RED}; }}
QPushButton#iconBtn {{ background: transparent; border-radius: 4px; padding: 4px; font-size: 13pt; min-width: 30px; }}
QPushButton#iconBtn:hover {{ background: {BG_HOVER}; }}
QPushButton#iconBtn:checked {{ color: {RED}; background: #3a2a2d; }}
QPushButton#guild {{ background: {BG2}; color: {TEXT}; border-radius: 24px; font-size: 13pt; padding: 0; }}
QPushButton#guild:hover {{ background: {ACCENT}; border-radius: 16px; }}
QPushButton#guild[unread="true"] {{ border: 2px solid white; }}
QPushButton#guild:checked {{ background: {ACCENT}; border-radius: 16px; }}
QPushButton#guildAdd {{ background: {BG2}; color: {GREEN}; border-radius: 24px; font-size: 18pt; padding: 0; }}
QPushButton#guildAdd:hover {{ background: {GREEN}; color: white; border-radius: 16px; }}
QPushButton#attach {{ background: {BG3}; color: {MUTED}; border-radius: 8px; font-size: 14pt; padding: 0; }}
QPushButton#attach:hover {{ color: {TEXT}; }}
QPushButton#chip {{ background: {BG1}; color: {TEXT}; border-radius: 4px; padding: 4px 8px; font-weight: normal; }}
QPushButton#chip:hover {{ background: {RED}; }}
QPushButton#fileBtn {{ background: {BG1}; color: #00a8fc; text-align: left; border: 1px solid #26272b; border-radius: 6px; padding: 10px 12px; font-weight: normal; }}
QPushButton#fileBtn:hover {{ background: {BG_HOVER}; }}
QPushButton#loadMore {{ background: transparent; color: {MUTED}; font-weight: normal; }}
QWidget#message:hover {{ background: #2e3035; }}
QProgressBar {{ background: {BG0}; border: none; border-radius: 3px; height: 8px; text-align: center; }}
QProgressBar::chunk {{ background: {GREEN}; border-radius: 3px; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 8px 14px; }}
QTabBar::tab:selected {{ color: white; border-bottom: 2px solid {ACCENT}; }}
QSlider::groove:horizontal {{ height: 6px; background: {BG0}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: white; width: 14px; margin: -5px 0; border-radius: 7px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}
QMenu {{ background: #111214; border: 1px solid #000; padding: 6px; }}
QMenu::item {{ padding: 6px 20px; border-radius: 3px; }}
QMenu::item:selected {{ background: {ACCENT}; }}
QToolTip {{ background: #111214; color: {TEXT}; border: none; padding: 6px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; }}
"""

NAME_COLORS = ["#f47b67", "#f8a532", "#48dd80", "#3fc7eb", "#7289da", "#e78cf1", "#f2d43b", "#4fdcc3", "#ff7a9c", "#a3a6ff"]


def name_color(key: str) -> str:
    h = int(hashlib.md5(key.encode()).hexdigest()[:6], 16)
    return NAME_COLORS[h % len(NAME_COLORS)]


def initials(name: str) -> str:
    parts = [p for p in re.split(r"[\s_\-.]+", name.strip()) if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper() if len(parts[0]) > 2 else parts[0].upper()
    return (parts[0][0] + parts[1][0]).upper()


def avatar_pixmap(name: str, size: int = 32, key: str | None = None) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(name_color(key or name)))
    p.setPen(Qt.NoPen)
    p.drawEllipse(0, 0, size, size)
    f = QFont()
    f.setBold(True)
    f.setPixelSize(int(size * 0.42))
    p.setFont(f)
    p.setPen(QColor("white"))
    p.drawText(pm.rect(), Qt.AlignCenter, initials(name)[:2])
    p.end()
    return pm


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, size, size), size * 0.28, size * 0.28)
        p.fillPath(path, QColor(ACCENT))
        f = QFont()
        f.setBold(True)
        f.setPixelSize(int(size * 0.62))
        p.setFont(f)
        p.setPen(QColor("white"))
        p.drawText(pm.rect(), Qt.AlignCenter, "Б")
        p.end()
        icon.addPixmap(pm)
    return icon


def fmt_time(ts: float) -> str:
    lt = time.localtime(ts)
    now = time.localtime()
    hm = time.strftime("%H:%M", lt)
    if lt.tm_yday == now.tm_yday and lt.tm_year == now.tm_year:
        return f"Сегодня в {hm}"
    y = time.localtime(time.time() - 86400)
    if lt.tm_yday == y.tm_yday and lt.tm_year == y.tm_year:
        return f"Вчера в {hm}"
    return time.strftime("%d.%m.%Y %H:%M", lt)


def fmt_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} Б"


_URL_RE = re.compile(r"(https?://[^\s<>\"']+)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?![\*\w])")
_CODE_RE = re.compile(r"`([^`\n]+)`")


def render_text(text: str, my_name: str | None = None) -> str:
    """Текст сообщения -> безопасный HTML: ссылки, **жирный**, *курсив*, `код`, @упоминания."""
    s = html.escape(text)
    s = _CODE_RE.sub(r'<code style="background:#1e1f22;padding:1px 4px;">\1</code>', s)
    s = _URL_RE.sub(r'<a href="\1" style="color:#00a8fc;">\1</a>', s)
    s = _BOLD_RE.sub(r"<b>\1</b>", s)
    s = _ITALIC_RE.sub(r"<i>\1</i>", s)
    if my_name:
        s = re.sub(r"(@" + re.escape(html.escape(my_name)) + r")\b",
                   r'<span style="background:#3c4270;color:#c9cdfb;">\1</span>', s, flags=re.IGNORECASE)
    return s.replace("\n", "<br>")

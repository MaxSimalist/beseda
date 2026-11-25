"""Настройки: звук, режим передачи, профиль, учётная запись."""
import sys

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMessageBox, QProgressBar, QPushButton, QRadioButton, QSlider,
                               QTabWidget, QVBoxLayout, QWidget)

from .. import codec
from ..voice import list_devices

IS_WIN = sys.platform == "win32"
BITRATES = [(24000, "24 кбит/с — слабый интернет"), (32000, "32 кбит/с"), (48000, "48 кбит/с — по умолчанию"),
            (64000, "64 кбит/с"), (96000, "96 кбит/с — музыка")]


def to_bool(v, default=False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("1", "true", "yes")


def apply_audio_settings(engine, s):
    """Прочитать настройки звука из QSettings в движок."""
    ins, outs = list_devices()
    in_label = s.value("audio/input", "")
    out_label = s.value("audio/output", "")
    engine.input_device = next((i for i, l in ins if l == in_label), None)
    engine.output_device = next((i for i, l in outs if l == out_label), None)
    engine.input_gain_db = float(s.value("audio/gain", 0.0))
    engine.threshold_db = float(s.value("audio/threshold", -50.0))
    engine.vad_enabled = to_bool(s.value("audio/vad"), True)
    engine.ptt_mode = IS_WIN and to_bool(s.value("audio/ptt"), False) and int(s.value("audio/ptt_vk", 0) or 0) > 0
    engine.output_volume = float(s.value("audio/volume", 1.0))
    engine.set_bitrate(int(s.value("audio/bitrate", 48000)))


class LevelMeter(QWidget):
    """Индикатор уровня микрофона с отметкой порога активации."""

    def __init__(self):
        super().__init__()
        self.bar = QProgressBar()
        self.bar.setRange(-60, 0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(10)
        self.label = QLabel("—")
        self.label.setObjectName("muted")
        self.label.setFixedWidth(70)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.bar, 1)
        lay.addWidget(self.label)

    def set_level(self, db: float, open_: bool):
        self.bar.setValue(int(max(-60, min(0, db))))
        self.label.setText(f"{db:5.1f} dB" if db > -100 else "—")
        color = "#23a55a" if open_ else "#80848e"
        if db > -3:
            color = "#da373c"
        self.bar.setStyleSheet(f"QProgressBar::chunk {{ background: {color}; border-radius: 3px; }}")


class SettingsDialog(QDialog):
    logout_requested = Signal()
    display_name_changed = Signal(str)

    def __init__(self, engine, settings, api, me: dict, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.s = settings
        self.api = api
        self.me = me
        self.setWindowTitle("Настройки")
        self.setMinimumSize(560, 520)
        self._capturing = False

        tabs = QTabWidget()
        tabs.addTab(self._voice_tab(), "Голос и звук")
        tabs.addTab(self._screen_tab(), "Экран и GIF")
        tabs.addTab(self._profile_tab(), "Профиль")
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        close = QPushButton("Готово")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)

        self._started_audio = False
        if not engine.audio_running:
            engine.start_audio()
            self._started_audio = True
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    # ------------------------------------------------------------ вкладка «Голос»
    def _voice_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setLabelAlignment(Qt.AlignRight)
        ins, outs = list_devices()
        self.in_combo = QComboBox()
        self.in_combo.addItem("По умолчанию (системное)", "")
        for _, label in ins:
            self.in_combo.addItem(label, label)
        self.out_combo = QComboBox()
        self.out_combo.addItem("По умолчанию (системное)", "")
        for _, label in outs:
            self.out_combo.addItem(label, label)
        self._select(self.in_combo, self.s.value("audio/input", ""))
        self._select(self.out_combo, self.s.value("audio/output", ""))
        self.in_combo.currentIndexChanged.connect(self._devices_changed)
        self.out_combo.currentIndexChanged.connect(self._devices_changed)
        form.addRow("Микрофон", self.in_combo)
        form.addRow("Выход", self.out_combo)

        self.gain = QSlider(Qt.Horizontal)
        self.gain.setRange(-20, 30)
        self.gain.setValue(int(self.engine.input_gain_db))
        self.gain_lbl = QLabel()
        self.gain.valueChanged.connect(self._gain)
        form.addRow("Усиление микрофона", self._with_label(self.gain, self.gain_lbl))

        self.meter = LevelMeter()
        form.addRow("Уровень", self.meter)

        self.vad_radio = QRadioButton("Активация голосом")
        self.open_radio = QRadioButton("Открытый микрофон")
        self.ptt_radio = QRadioButton("Рация (Push-to-Talk)")
        mode = "ptt" if self.engine.ptt_mode else ("vad" if self.engine.vad_enabled else "open")
        {"ptt": self.ptt_radio, "vad": self.vad_radio, "open": self.open_radio}[mode].setChecked(True)
        if not IS_WIN:
            self.ptt_radio.setEnabled(False)
            self.ptt_radio.setToolTip("Глобальная клавиша рации работает только в Windows")
        modes = QVBoxLayout()
        for r in (self.vad_radio, self.open_radio, self.ptt_radio):
            r.toggled.connect(self._mode_changed)
            modes.addWidget(r)
        form.addRow("Режим передачи", modes)

        self.thr = QSlider(Qt.Horizontal)
        self.thr.setRange(-70, -10)
        self.thr.setValue(int(self.engine.threshold_db))
        self.thr_lbl = QLabel()
        self.thr.valueChanged.connect(self._thr)
        self.thr_row = self._with_label(self.thr, self.thr_lbl)
        form.addRow("Порог активации", self.thr_row)

        self.ptt_btn = QPushButton()
        self.ptt_btn.setObjectName("flat")
        self.ptt_btn.clicked.connect(self._capture_key)
        self._ptt_text()
        form.addRow("Клавиша рации", self.ptt_btn)

        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 200)
        self.vol.setValue(int(self.engine.output_volume * 100))
        self.vol_lbl = QLabel()
        self.vol.valueChanged.connect(self._vol)
        form.addRow("Громкость выхода", self._with_label(self.vol, self.vol_lbl))

        self.br = QComboBox()
        for bps, label in BITRATES:
            self.br.addItem(label, bps)
        self._select(self.br, self.engine.bitrate)
        self.br.currentIndexChanged.connect(self._bitrate)
        form.addRow("Качество голоса", self.br)

        self.loop = QCheckBox("Слышать себя (проверка микрофона)")
        self.loop.setChecked(self.engine.loopback)
        self.loop.toggled.connect(lambda on: setattr(self.engine, "loopback", on))
        form.addRow("", self.loop)

        if codec.OPUS_AVAILABLE:
            c = QLabel(f"Кодек: Opus ({codec.opus_version()})")
            c.setObjectName("muted")
        else:
            c = QLabel("⚠ Opus не найден — используется запасной кодек (хуже качество, больше трафик). "
                       "Запустите tools/get_opus.py.")
            c.setStyleSheet("color: #f0b232;")
            c.setWordWrap(True)
        form.addRow("", c)
        self.err = QLabel("")
        self.err.setStyleSheet("color: #fa777c;")
        self.err.setWordWrap(True)
        form.addRow("", self.err)
        self._gain(self.gain.value())
        self._thr(self.thr.value())
        self._vol(self.vol.value())
        self._mode_changed()
        return w

    @staticmethod
    def _with_label(slider, label) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        label.setFixedWidth(70)
        label.setObjectName("muted")
        h.addWidget(slider, 1)
        h.addWidget(label)
        return w

    @staticmethod
    def _select(combo, value):
        i = combo.findData(value)
        combo.setCurrentIndex(i if i >= 0 else 0)

    def _devices_changed(self):
        self.s.setValue("audio/input", self.in_combo.currentData())
        self.s.setValue("audio/output", self.out_combo.currentData())
        ins, outs = list_devices()
        self.engine.input_device = next((i for i, l in ins if l == self.in_combo.currentData()), None)
        self.engine.output_device = next((i for i, l in outs if l == self.out_combo.currentData()), None)
        self.engine.restart_audio()

    def _gain(self, v):
        self.engine.input_gain_db = float(v)
        self.gain_lbl.setText(f"{v:+d} dB")
        self.s.setValue("audio/gain", v)

    def _thr(self, v):
        self.engine.threshold_db = float(v)
        self.thr_lbl.setText(f"{v} dB")
        self.s.setValue("audio/threshold", v)

    def _vol(self, v):
        self.engine.output_volume = v / 100.0
        self.vol_lbl.setText(f"{v} %")
        self.s.setValue("audio/volume", v / 100.0)

    def _bitrate(self):
        bps = int(self.br.currentData())
        self.engine.set_bitrate(bps)
        self.s.setValue("audio/bitrate", bps)

    def _mode_changed(self):
        ptt = self.ptt_radio.isChecked()
        vad = self.vad_radio.isChecked()
        vk = int(self.s.value("audio/ptt_vk", 0) or 0)
        self.engine.vad_enabled = vad
        self.engine.ptt_mode = ptt and vk > 0
        self.s.setValue("audio/vad", vad)
        self.s.setValue("audio/ptt", ptt)
        self.thr_row.setEnabled(vad)
        self.ptt_btn.setEnabled(ptt)
        if ptt and vk == 0:
            self._capture_key()

    # --- клавиша рации ---
    def _ptt_text(self):
        name = self.s.value("audio/ptt_name", "")
        self.ptt_btn.setText(f"⌨ {name} — изменить" if name else "⌨ Назначить клавишу…")

    def _capture_key(self):
        self._capturing = True
        self.ptt_btn.setText("Нажмите клавишу или боковую кнопку мыши…  (Esc — отмена)")
        self.grabKeyboard()

    def _set_ptt(self, vk: int, name: str):
        self._capturing = False
        self.releaseKeyboard()
        if vk:
            self.s.setValue("audio/ptt_vk", vk)
            self.s.setValue("audio/ptt_name", name)
        self._ptt_text()
        self._mode_changed()

    def keyPressEvent(self, e):
        if self._capturing:
            if e.key() == Qt.Key_Escape:
                self._set_ptt(0, "")
            else:
                vk = e.nativeVirtualKey()
                name = QKeySequence(e.key()).toString() or f"VK {vk}"
                self._set_ptt(vk, name)
            return
        super().keyPressEvent(e)

    def mousePressEvent(self, e):
        if self._capturing:
            vk = {Qt.XButton1: 0x05, Qt.XButton2: 0x06, Qt.MiddleButton: 0x04}.get(e.button())
            if vk:
                name = {0x05: "Мышь X1", 0x06: "Мышь X2", 0x04: "Средняя кнопка мыши"}[vk]
                self._set_ptt(vk, name)
                return
        super().mousePressEvent(e)

    # ------------------------------------------------------------ вкладка «Экран и GIF»
    def _screen_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)

        fps = QComboBox()
        for f, label in ((10, "10 к/с — слабый интернет"), (15, "15 к/с — по умолчанию"), (30, "30 к/с — плавно")):
            fps.addItem(label, f)
        self._select(fps, int(self.s.value("cast/fps", 15) or 15))
        fps.currentIndexChanged.connect(
            lambda _: self.s.setValue("cast/fps", fps.currentData()))
        form.addRow("Частота кадров", fps)

        br = QComboBox()
        for label, bps in (("Низкое — 1 Мбит/с", 1_000_000), ("Среднее — 2 Мбит/с", 2_000_000),
                           ("Высокое — 4 Мбит/с", 4_000_000)):
            br.addItem(label, bps)
        self._select(br, int(self.s.value("cast/bitrate", 2_000_000) or 2_000_000))
        br.currentIndexChanged.connect(
            lambda _: self.s.setValue("cast/bitrate", br.currentData()))
        form.addRow("Качество трансляции", br)

        note = QLabel("Демонстрация запускается кнопкой 🖥 в голосовой панели "
                      "(нужно быть подключённым к голосовому каналу).")
        note.setWordWrap(True)
        note.setObjectName("muted")
        form.addRow(note)

        key = QLineEdit(self.s.value("giphy_key", ""))
        key.setPlaceholderText("API-ключ GIPHY — developers.giphy.com → Create an App → API")
        key.editingFinished.connect(lambda: self.s.setValue("giphy_key", key.text().strip()))
        form.addRow("Ключ GIPHY (гифки)", key)
        hint = QLabel("Бесплатный ключ: developers.giphy.com → Create an App → выберите «API». "
                      "Без ключа кнопка GIF откроет окно с инструкцией.")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        form.addRow(hint)
        return w

    # ------------------------------------------------------------ вкладка «Профиль»
    def _profile_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setLabelAlignment(Qt.AlignRight)
        form.addRow("Логин", QLabel(self.me.get("username", "")))
        self.dn = QLineEdit(self.me.get("display_name", ""))
        self.dn.setMaxLength(32)
        save = QPushButton("Сохранить")
        save.clicked.connect(lambda: self.display_name_changed.emit(self.dn.text().strip()))
        row = QHBoxLayout()
        row.addWidget(self.dn, 1)
        row.addWidget(save)
        form.addRow("Отображаемое имя", row)
        pw = QPushButton("Сменить пароль…")
        pw.setObjectName("flat")
        pw.clicked.connect(self._change_password)
        form.addRow("", pw)
        if self.api.base.startswith("http://"):
            mark = "  ⚠ без шифрования"
        elif self.api.insecure_ssl:
            mark = "  ⚠ самоподписанный сертификат"
        else:
            mark = "  🔒"
        server = QLabel(self.api.base + mark)
        server.setObjectName("muted")
        form.addRow("Сервер", server)
        out = QPushButton("Выйти из аккаунта")
        out.setObjectName("danger")
        out.clicked.connect(self._logout)
        form.addRow("", out)
        return w

    def _change_password(self):
        from ..api import run_bg
        old, ok = QInputDialog.getText(self, "Смена пароля", "Текущий пароль:", QLineEdit.Password)
        if not ok:
            return
        new, ok = QInputDialog.getText(self, "Смена пароля", "Новый пароль (от 6 символов):", QLineEdit.Password)
        if not ok:
            return
        run_bg(self.api.change_password, old, new,
               on_done=lambda _: QMessageBox.information(self, "Готово", "Пароль изменён"),
               on_fail=lambda e: QMessageBox.warning(self, "Ошибка", e))

    def _logout(self):
        if QMessageBox.question(self, "Выход", "Выйти из аккаунта на этом компьютере?") == QMessageBox.Yes:
            self.logout_requested.emit()
            self.accept()

    # ------------------------------------------------------------ общее
    def _tick(self):
        self.meter.set_level(self.engine.input_level_db, self.engine.gate_open)
        self.err.setText(self.engine.last_error)

    def done(self, r):
        self.timer.stop()
        if self._capturing:
            self.releaseKeyboard()
        self.engine.loopback = False
        if self._started_audio and not self.engine.in_voice:
            self.engine.stop_audio()
        super().done(r)

import re
from urllib.parse import urlparse
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QFormLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout)
from ..api import Api, run_bg
from .style import app_icon

IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

class LoginDialog(QDialog):
    def __init__(self, settings, parent=None, message=""):
        super().__init__(parent)
        self.settings = settings
        self.api = None
        self.result_user = None
        self.setWindowTitle("Беседа — вход")
        self.setWindowIcon(app_icon())
        self.setMinimumWidth(440)
        title = QLabel("С возвращением!")
        title.setStyleSheet("font-size: 18pt; font-weight: 700;")
        title.setAlignment(Qt.AlignCenter)
        sub = QLabel(message or "Рады видеть вас снова")
        sub.setObjectName("muted")
        sub.setAlignment(Qt.AlignCenter)
        sub.setWordWrap(True)

        self.server = QLineEdit(settings.value("server", "https://"))
        self.server.setPlaceholderText("https://chat.example.ru или https://1.2.3.4:8443")
        self.username = QLineEdit(settings.value("username", ""))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.register = QCheckBox("Создать новый аккаунт")
        self.code = QLineEdit()
        self.code.setPlaceholderText("если сервер его требует")
        self.code_label = QLabel("Код приглашения")
        self.password2 = QLineEdit()
        self.password2.setEchoMode(QLineEdit.Password)
        self.password2_label = QLabel("Повторите пароль")

        self.insecure_ssl = QCheckBox("Игнорировать ошибки SSL (для самоподписанных)")
        self.insecure_ssl.setChecked(str(settings.value("insecure_ssl", "false")).lower() in ("1", "true", "yes"))

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.addRow("Сервер", self.server)
        form.addRow("Логин", self.username)
        form.addRow("Пароль", self.password)
        form.addRow(self.password2_label, self.password2)
        form.addRow(self.code_label, self.code)

        self.error = QLabel("")
        self.error.setStyleSheet("color: #fa777c;")
        self.error.setWordWrap(True)
        self.go = QPushButton("Войти")
        self.go.setDefault(True)
        self.go.clicked.connect(self.submit)
        self.register.toggled.connect(self._mode)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 24)
        lay.setSpacing(10)
        lay.addWidget(title)
        lay.addWidget(sub)
        lay.addSpacing(8)
        lay.addLayout(form)
        lay.addWidget(self.register)
        lay.addWidget(self.insecure_ssl)
        lay.addWidget(self.error)
        lay.addWidget(self.go)
        self._mode(False)
        (self.password if self.username.text() else self.username).setFocus()
        self.server.textChanged.connect(self._server_changed)
        self._server_changed(self.server.text())

    def _server_changed(self, text):
        t = text.strip()
        if not t or t in ("https://", "http://"):
            return
        if "://" not in t:
            t = "https://" + t
        try:
            host = urlparse(t).hostname or ""
        except Exception:
            host = ""
        if IP_RE.match(host):
            self.insecure_ssl.setChecked(True)

    def _mode(self, reg):
        for w in (self.code, self.code_label, self.password2, self.password2_label):
            w.setVisible(reg)
        self.go.setText("Зарегистрироваться" if reg else "Войти")
        self.adjustSize()

    def submit(self):
        server = self.server.text().strip()
        user = self.username.text().strip()
        pw = self.password.text()
        if not server or server in ("https://", "http://") or not user or not pw:
            self.error.setText("Заполните адрес сервера, логин и пароль")
            return
        if self.register.isChecked() and pw != self.password2.text():
            self.error.setText("Пароли не совпадают")
            return
        self.api = Api(server, insecure_ssl=self.insecure_ssl.isChecked())
        self.go.setEnabled(False)
        self.error.setText("Подключение…")
        if self.register.isChecked():
            run_bg(self.api.register, user, pw, self.code.text().strip(), on_done=self._ok, on_fail=self._fail)
        else:
            run_bg(self.api.login, user, pw, on_done=self._ok, on_fail=self._fail)

    def _ok(self, data):
        self.result_user = data["user"]
        self.settings.setValue("server", self.api.base)
        self.settings.setValue("username", data["user"]["username"])
        self.settings.setValue("token", self.api.token)
        self.settings.setValue("insecure_ssl", self.insecure_ssl.isChecked())
        self.accept()

    def _fail(self, text):
        self.go.setEnabled(True)
        self.error.setText(text)

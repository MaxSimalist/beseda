import logging
import os
import sys
import traceback
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QDialog
from .api import Api
from .ui.login import LoginDialog
from .ui.main_window import MainWindow
from .ui.style import QSS, app_icon

def profile_name(argv):
    for i, a in enumerate(argv):
        if a == "--profile" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--profile="):
            return a.split("=", 1)[1]
    return ""

class Controller:
    def __init__(self, app, profile=""):
        self.app = app
        self.s = QSettings("Beseda", f"Beseda-{profile}" if profile else "Beseda")
        self.win = None

    def start(self) -> bool:
        token = self.s.value("token")
        server = self.s.value("server")
        insecure_ssl = str(self.s.value("insecure_ssl", "false")).lower() in ("1", "true", "yes")
        if token and server:
            self.open_main(Api(server, token, insecure_ssl=insecure_ssl))
            return True
        return self.login()

    def login(self, message=""):
        dlg = LoginDialog(self.s, message=message)
        if dlg.exec() == QDialog.Accepted and dlg.api:
            self.open_main(dlg.api)
            return True
        return False

    def open_main(self, api):
        if self.win:
            self.win.shutdown()
            self.win.deleteLater()
        self.win = MainWindow(api, self.s)
        self.win.logged_out.connect(lambda: self._relogin(""))
        self.win.auth_failed.connect(lambda reason: self._relogin(reason or "Сессия устарела — войдите снова"))
        self.win.show()

    def _relogin(self, message):
        self.s.remove("token")
        if self.win:
            self.win.hide()
            self.win.shutdown()
            self.win.deleteLater()
            self.win = None
        if not self.login(message):
            self.app.quit()

def setup_logging():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    d = os.path.join(base, "Beseda")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "beseda.log")
    handlers = [logging.FileHandler(path, encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        logging.getLogger("beseda").error("Необработанная ошибка:\n%s", text)
        try:
            from PySide6.QtWidgets import QMessageBox
            if QApplication.instance():
                QMessageBox.critical(None, "Ошибка", f"{exc_type.__name__}: {exc}\n\nПодробности в журнале:\n{path}")
        except Exception:
            pass
    sys.excepthook = hook
    return path

def main():
    log_path = setup_logging()
    logging.getLogger("beseda").info("Запуск клиента, журнал: %s", log_path)
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("Беседа")
    app.setWindowIcon(app_icon())
    app.setStyleSheet(QSS)
    app.setQuitOnLastWindowClosed(False)
    profile = profile_name(sys.argv)
    if profile:
        app.setApplicationDisplayName(f"Беседа [{profile}]")
    ctl = Controller(app, profile)
    if not ctl.start():
        return
    sys.exit(app.exec())

if __name__ == "__main__":
    main()

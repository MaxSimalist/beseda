"""Скачивает готовую libopus для Windows (64 бит) и кладёт рядом с клиентом.

Библиотеку берём из колеса discord.py на PyPI — там лежит собранная libopus-0.x64.dll.
Запуск: python tools/get_opus.py
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(os.path.dirname(HERE), "libopus-0.x64.dll")


def is_x64(data: bytes) -> bool:
    try:
        pe = int.from_bytes(data[0x3C:0x40], "little")
        return data[pe:pe + 4] == b"PE\0\0" and int.from_bytes(data[pe + 4:pe + 6], "little") == 0x8664
    except Exception:
        return False


def main():
    if os.path.exists(DEST):
        print("Уже есть:", DEST)
        return
    tmp = tempfile.mkdtemp()
    print("Скачиваю discord.py с PyPI (только ради libopus)…")
    r = subprocess.run([sys.executable, "-m", "pip", "download", "discord.py", "--no-deps",
                        "--only-binary=:all:", "-d", tmp, "-q"])
    wheels = glob.glob(os.path.join(tmp, "*.whl"))
    if r.returncode != 0 or not wheels:
        fail()
    with zipfile.ZipFile(wheels[0]) as z:
        for name in z.namelist():
            base = os.path.basename(name).lower()
            if base.startswith("libopus") and base.endswith(".dll"):
                data = z.read(name)
                if is_x64(data):
                    with open(DEST, "wb") as f:
                        f.write(data)
                    print("Готово:", DEST)
                    shutil.rmtree(tmp, ignore_errors=True)
                    return
    fail()


def fail():
    print("\nНе удалось достать libopus автоматически. Вручную:\n"
          "  1) pip install discord.py\n"
          "  2) найдите в site-packages\\discord\\bin\\libopus-0.x64.dll\n"
          f"  3) скопируйте её в {os.path.dirname(DEST)}\n"
          "Без неё голос работает на запасном кодеке (хуже качество).")
    sys.exit(1)


if __name__ == "__main__":
    main()

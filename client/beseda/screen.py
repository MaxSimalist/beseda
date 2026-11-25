"""Демонстрация экрана: захват (bettercam/mss) -> H.264 (PyAV) -> куски для UDP.

И обратно: сборка кадров из кусков -> декодирование H.264 -> QImage для интерфейса.

Всё работает в своих потоках, Qt не используется (QImage — это не GUI).
"""
import logging
import threading
import time

import numpy as np

log = logging.getLogger("beseda.screen")

try:
    import av
except Exception:  # pragma: no cover
    av = None

CHUNK = 1100          # полезная нагрузка одного UDP-пакета (без заголовков протокола)
FPS_CHOICES = (10, 15, 30)
BITRATE_CHOICES = {"Низкое (1 Мбит/с)": 1_000_000, "Среднее (2 Мбит/с)": 2_000_000,
                   "Высокое (4 Мбит/с)": 4_000_000}


def available() -> bool:
    return av is not None


def list_monitors() -> list[tuple[int, str]]:
    """[(index, 'Монитор 1 — 1920×1080'), ...]."""
    out = []
    try:
        import mss
        with mss.mss() as sct:
            for i, m in enumerate(sct.monitors[1:], 0):
                out.append((i, f"Монитор {i + 1} — {m['width']}×{m['height']}"))
    except Exception as e:
        log.warning("не удалось получить список мониторов: %s", e)
    return out


class _Capture:
    """Захват кадров через mss."""

    def __init__(self, monitor: int):
        import mss
        self._sct = mss.mss()
        idx = monitor + 1
        if idx >= len(self._sct.monitors):
            idx = 1
        self._mon = self._sct.monitors[idx]

    def grab(self) -> np.ndarray | None:
        """BGRA-кадр."""
        shot = self._sct.grab(self._mon)
        return np.asarray(shot, dtype=np.uint8)[:, :, :4]  # BGRA

    def close(self):
        try:
            self._sct.close()
        except Exception:
            pass


class ScreenCaster:
    """Поток: захват -> H.264 -> on_chunk(plain-payload) для каждого куска кадра."""

    def __init__(self, monitor: int, fps: int, bitrate: int, on_chunk, on_error=None):
        self.monitor = monitor
        self.fps = max(5, min(30, fps))
        self.bitrate = bitrate
        self.on_chunk = on_chunk          # вызывается из потока! (bytes_payload, key: bool)
        self.on_error = on_error
        self.frames_sent = 0
        self.bytes_sent = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame_seq = 0

    def start(self):
        self._thread = threading.Thread(target=self._run, name="screen-cast", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t is not threading.current_thread():
            t.join(timeout=2.0)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def _run(self):
        try:
            self._encode_loop()
        except Exception as e:
            log.exception("трансляция экрана прервана")
            if self.on_error:
                try:
                    self.on_error(str(e))
                except Exception:
                    pass

    def _encode_loop(self):
        if av is None:
            raise RuntimeError("Не установлен пакет av (PyAV)")
        from fractions import Fraction
        cap = _Capture(self.monitor)
        codec = None
        period = 1.0 / self.fps
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                frame = cap.grab()
                if frame is None:
                    time.sleep(0.005)
                    continue
                h, w = frame.shape[:2]
                w2, h2 = w & ~1, h & ~1                    # H.264 хочет чётные размеры
                if codec is None:
                    codec = av.CodecContext.create("libx264", "w")
                    codec.width, codec.height = w2, h2
                    codec.pix_fmt = "yuv420p"
                    codec.bit_rate = self.bitrate
                    codec.framerate = Fraction(self.fps, 1)
                    codec.time_base = Fraction(1, self.fps)
                    codec.options = {"preset": "ultrafast", "tune": "zerolatency",
                                     "g": str(self.fps), "sc_threshold": "0"}
                vf = frame[:h2, :w2, :3]                   # BGR
                img = av.VideoFrame.from_ndarray(vf, format="bgr24")
                for pkt in codec.encode(img):
                    self._emit(bytes(pkt), bool(pkt.is_keyframe))
                dt = time.monotonic() - t0
                if dt < period:
                    time.sleep(period - dt)
            if codec is not None:                          # дожать хвост
                for pkt in codec.encode(None):
                    self._emit(bytes(pkt), bool(pkt.is_keyframe))
        finally:
            cap.close()

    def _emit(self, data: bytes, key: bool):
        if not data:
            return
        self._frame_seq = (self._frame_seq + 1) & 0xFFFF
        cnt = (len(data) + CHUNK - 1) // CHUNK
        if cnt > 255:
            return                                        # абсурдно большой кадр — пропускаем
        for idx in range(cnt):
            chunk = data[idx * CHUNK:(idx + 1) * CHUNK]
            payload = _pack_frag(self._frame_seq, idx, cnt, key) + chunk
            try:
                self.on_chunk(payload, key)
            except Exception:
                log.debug("отправка куска не удалась", exc_info=True)
                return
        self.frames_sent += 1
        self.bytes_sent += len(data)


# ---------------------------------------------------------------- приём
def _pack_frag(seq: int, idx: int, cnt: int, key: bool) -> bytes:
    import struct
    return struct.pack(">HBBB", seq, idx, cnt, 1 if key else 0)


def parse_frag(payload: bytes):
    """-> (frame_seq, idx, cnt, key, chunk) или None."""
    import struct
    if len(payload) < 5:
        return None
    seq, idx, cnt, key = struct.unpack_from(">HBBB", payload, 0)
    if cnt == 0 or idx >= cnt:
        return None
    return seq, idx, cnt, bool(key), payload[5:]


class FrameAssembler:
    """Собирает кадры из кусков. Потерян кусок — кадр выбрасывается
    (следующий ключевой кадр придёт через ~1 с, GOP короткий)."""

    def __init__(self):
        self.parts: dict[int, dict] = {}

    def feed(self, payload: bytes) -> tuple[bytes, bool] | None:
        f = parse_frag(payload)
        if not f:
            return None
        seq, idx, cnt, key, chunk = f
        now = time.monotonic()
        for old_seq in [s for s, p in self.parts.items() if now - p["t"] > 1.0]:
            self.parts.pop(old_seq, None)
        p = self.parts.get(seq)
        if p is None:
            p = self.parts[seq] = {"t": now, "cnt": cnt, "key": key, "got": 0, "buf": [None] * cnt}
        if p["cnt"] != cnt or p["buf"][idx] is not None:
            return None
        p["buf"][idx] = chunk
        p["got"] += 1
        if p["got"] < p["cnt"]:
            return None
        self.parts.pop(seq, None)
        return b"".join(p["buf"]), p["key"]


class VideoDecoder:
    """H.264 -> QImage. decode() вызывать из одного потока (нашего recv/воркера)."""

    def __init__(self):
        self.codec = av.CodecContext.create("h264", "r") if av else None

    def decode(self, data: bytes):
        """-> QImage или None."""
        if self.codec is None:
            return None
        from PySide6.QtGui import QImage
        try:
            for frame in self.codec.decode(av.Packet(data)):
                rgb = frame.to_ndarray(format="rgb24")
                h, w = rgb.shape[:2]
                img = QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888)
                return img.copy()                          # свои данные, буфер можно освободить
        except Exception:
            log.debug("ошибка декодирования кадра", exc_info=True)
            self.codec = av.CodecContext.create("h264", "r")   # рассинхрон — начнём с чистого
        return None

"""Аудиокодеки для голоса.

Основной — Opus через ctypes (нужен libopus-0.dll / opus.dll рядом с программой,
скачивается скриптом tools/get_opus.py). Если библиотеки нет — запасной
G.711 μ-law 16 кГц: работает везде, но звучит хуже и ест ~130 кбит/с.
"""
import ctypes
import ctypes.util
import os
import sys
from pathlib import Path

import numpy as np

SAMPLE_RATE = 48000
FRAME = 960  # 20 мс при 48 кГц

CODEC_OPUS = 0
CODEC_ULAW16 = 1

OPUS_APPLICATION_VOIP = 2048
OPUS_SET_BITRATE = 4002
OPUS_SET_INBAND_FEC = 4012
OPUS_SET_PACKET_LOSS_PERC = 4014


def _app_dirs() -> list[Path]:
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)))
        dirs.append(Path(sys.executable).parent)
    here = Path(__file__).resolve().parent
    dirs += [here, here.parent, here.parent / "lib"]
    return dirs


def _load_opus():
    names = ["libopus-0.x64.dll", "libopus-0.dll", "opus.dll", "libopus.dll"] if os.name == "nt" \
        else ["libopus.so.0", "libopus.so", "libopus.0.dylib", "libopus.dylib"]
    candidates = []
    if os.environ.get("BESEDA_OPUS"):
        candidates.append(os.environ["BESEDA_OPUS"])
    for d in _app_dirs():
        candidates += [str(d / n) for n in names]
    candidates += names
    found = ctypes.util.find_library("opus")
    if found:
        candidates.append(found)
    for c in candidates:
        try:
            lib = ctypes.CDLL(c)
            lib.opus_get_version_string.restype = ctypes.c_char_p
            return lib, c
        except OSError:
            continue
    return None, None


_lib, OPUS_PATH = _load_opus()
OPUS_AVAILABLE = _lib is not None

if _lib is not None:
    _lib.opus_encoder_create.restype = ctypes.c_void_p
    _lib.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    _lib.opus_encode.restype = ctypes.c_int32
    _lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int32]
    _lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
    _lib.opus_decoder_create.restype = ctypes.c_void_p
    _lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    _lib.opus_decode.restype = ctypes.c_int
    _lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    _lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
    _lib.opus_encoder_ctl.restype = ctypes.c_int
    _lib.opus_encoder_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int32]


def opus_version() -> str:
    return _lib.opus_get_version_string().decode() if _lib else "нет"


# ---------------------------------------------------------------- Opus
class OpusEncoder:
    codec_id = CODEC_OPUS

    def __init__(self, bitrate: int = 48000):
        err = ctypes.c_int()
        self._st = _lib.opus_encoder_create(SAMPLE_RATE, 1, OPUS_APPLICATION_VOIP, ctypes.byref(err))
        if err.value != 0 or not self._st:
            raise RuntimeError(f"opus_encoder_create: {err.value}")
        self.set_bitrate(bitrate)
        _lib.opus_encoder_ctl(self._st, OPUS_SET_INBAND_FEC, 1)
        _lib.opus_encoder_ctl(self._st, OPUS_SET_PACKET_LOSS_PERC, 5)
        self._out = ctypes.create_string_buffer(1275)

    def set_bitrate(self, bitrate: int):
        _lib.opus_encoder_ctl(self._st, OPUS_SET_BITRATE, int(bitrate))

    def encode(self, pcm: np.ndarray) -> bytes:
        """pcm — int16, FRAME сэмплов."""
        pcm = np.ascontiguousarray(pcm, dtype=np.int16)
        n = _lib.opus_encode(self._st, pcm.ctypes.data, FRAME, self._out, len(self._out))
        if n < 0:
            raise RuntimeError(f"opus_encode: {n}")
        return self._out.raw[:n]

    def __del__(self):
        if getattr(self, "_st", None):
            _lib.opus_encoder_destroy(self._st)
            self._st = None


class OpusDecoder:
    def __init__(self):
        err = ctypes.c_int()
        self._st = _lib.opus_decoder_create(SAMPLE_RATE, 1, ctypes.byref(err))
        if err.value != 0 or not self._st:
            raise RuntimeError(f"opus_decoder_create: {err.value}")
        self._pcm = np.zeros(FRAME * 6, dtype=np.int16)

    def decode(self, data: bytes | None, fec: bool = False) -> np.ndarray:
        """data=None — маскировка потерянного пакета (PLC)."""
        if data is None:
            n = _lib.opus_decode(self._st, None, 0, self._pcm.ctypes.data, FRAME, 0)
        else:
            buf = ctypes.create_string_buffer(data, len(data))
            n = _lib.opus_decode(self._st, buf, len(data), self._pcm.ctypes.data, FRAME if fec else FRAME * 6, 1 if fec else 0)
        if n < 0:
            return np.zeros(FRAME, dtype=np.int16)
        return self._pcm[:n].copy()

    def __del__(self):
        if getattr(self, "_st", None):
            _lib.opus_decoder_destroy(self._st)
            self._st = None


# ---------------------------------------------------------------- μ-law 16 кГц (запасной)
def _ulaw_encode(x: np.ndarray) -> np.ndarray:
    BIAS, CLIP = 0x84, 32635
    s = x.astype(np.int32)
    sign = np.where(s < 0, 0x80, 0)
    m = np.minimum(np.abs(s), CLIP) + BIAS
    exp = np.floor(np.log2(m)).astype(np.int32) - 7
    exp = np.clip(exp, 0, 7)
    mant = (m >> (exp + 3)) & 0x0F
    return (~(sign | (exp << 4) | mant) & 0xFF).astype(np.uint8)


def _build_ulaw_table() -> np.ndarray:
    u = ~np.arange(256, dtype=np.int32) & 0xFF
    sign = u & 0x80
    exp = (u >> 4) & 0x07
    mant = u & 0x0F
    mag = (((mant << 3) + 0x84) << exp) - 0x84
    return np.where(sign != 0, -mag, mag).astype(np.int16)


_ULAW_TABLE = _build_ulaw_table()


class UlawEncoder:
    codec_id = CODEC_ULAW16

    def set_bitrate(self, bitrate: int):
        pass

    def encode(self, pcm: np.ndarray) -> bytes:
        # 48 кГц -> 16 кГц: усреднение по 3 сэмпла (простой ФНЧ + прореживание)
        x = pcm.astype(np.int32).reshape(-1, 3).mean(axis=1)
        return _ulaw_encode(x.astype(np.int16)).tobytes()


class UlawDecoder:
    def __init__(self):
        self._last = np.zeros(FRAME, dtype=np.int16)

    def decode(self, data: bytes | None, fec: bool = False) -> np.ndarray:
        if data is None:
            self._last = (self._last * 0.5).astype(np.int16)
            return self._last
        y = _ULAW_TABLE[np.frombuffer(data, dtype=np.uint8)].astype(np.float32)
        xp = np.arange(len(y)) * 3
        out = np.interp(np.arange(len(y) * 3), xp, y).astype(np.int16)
        self._last = out
        return out


def make_encoder(bitrate: int = 48000):
    return OpusEncoder(bitrate) if OPUS_AVAILABLE else UlawEncoder()


def make_decoder(codec_id: int):
    if codec_id == CODEC_OPUS:
        return OpusDecoder() if OPUS_AVAILABLE else None
    if codec_id == CODEC_ULAW16:
        return UlawDecoder()
    return None

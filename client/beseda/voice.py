"""Голосовой движок клиента: микрофон -> Opus -> шифрование -> UDP, и обратно с микшированием.

Работает в своих потоках (callback'и sounddevice + поток отправки + поток приёма),
GUI только читает его состояние таймером. Qt здесь не используется — так его
можно тестировать без интерфейса.
"""
import logging
import math
import queue
import socket
import struct
import threading
import time
from collections import deque

import numpy as np

from . import codec
from .codec import FRAME, SAMPLE_RATE

try:
    import sounddevice as sd
except Exception:  # pragma: no cover - нет PortAudio
    sd = None

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.exceptions import InvalidTag

log = logging.getLogger("beseda.voice")

KIND_HELLO = 0
KIND_AUDIO = 1
SELF_ID = 0  # «говорящий» для самопрослушивания

PREBUFFER = 3       # кадров (60 мс) копим перед началом воспроизведения говорящего
MAX_BUFFER = 8      # больше 160 мс — выбрасываем кадр, чтобы не росла задержка
SPEAK_HOLD = 0.3    # сек, сколько держится индикатор «говорит»


# ---------------------------------------------------------------- протокол
class VoiceCrypto:
    def __init__(self, ssrc: int, key: bytes):
        self.ssrc = ssrc
        self.ssrc_bytes = struct.pack(">I", ssrc)
        self.aead = ChaCha20Poly1305(key)
        self.tx = 0
        self.rx = 0
        self.lock = threading.Lock()

    def seal(self, plain: bytes) -> bytes:
        with self.lock:
            self.tx += 1
            c = struct.pack(">Q", self.tx)
        return self.ssrc_bytes + c + self.aead.encrypt(b"C\0\0\0" + c, plain, self.ssrc_bytes)

    def open(self, data: bytes) -> bytes | None:
        if len(data) < 8 + 16 + 1:
            return None
        counter = struct.unpack_from(">Q", data, 0)[0]
        if counter <= self.rx:
            return None
        try:
            plain = self.aead.decrypt(b"S\0\0\0" + data[:8], data[8:], b"")
        except InvalidTag:
            return None
        self.rx = counter
        return plain


# ---------------------------------------------------------------- устройства
def list_devices() -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """[(index, 'Имя — API')] для входов и выходов."""
    if sd is None:
        return [], []
    ins, outs = [], []
    try:
        apis = sd.query_hostapis()
        for i, d in enumerate(sd.query_devices()):
            api = apis[d["hostapi"]]["name"]
            label = f"{d['name']} — {api}"
            if d["max_input_channels"] > 0:
                ins.append((i, label))
            if d["max_output_channels"] > 0:
                outs.append((i, label))
    except Exception as e:
        log.warning("не удалось получить список устройств: %s", e)
    return ins, outs


def _pick_rate(device, kind: str) -> int:
    """48 кГц, если устройство умеет, иначе его родная частота."""
    check = sd.check_input_settings if kind == "input" else sd.check_output_settings
    try:
        check(device=device, samplerate=SAMPLE_RATE, channels=1, dtype="float32")
        return SAMPLE_RATE
    except Exception:
        info = sd.query_devices(device, kind)
        return int(info["default_samplerate"])


def _resample(x: np.ndarray, n_out: int) -> np.ndarray:
    if len(x) == n_out:
        return x
    xp = np.linspace(0.0, 1.0, num=len(x), endpoint=False)
    xo = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(xo, xp, x).astype(np.float32)


# ---------------------------------------------------------------- говорящий
class Speaker:
    def __init__(self, codec_id: int):
        self.codec_id = codec_id
        self.decoder = codec.make_decoder(codec_id)
        self.frames: deque = deque()
        self.expected: int | None = None
        self.playing = False
        self.last_rx = 0.0
        self.last_audible = 0.0


# ---------------------------------------------------------------- движок
class VoiceEngine:
    def __init__(self):
        self.lock = threading.RLock()
        # настройки
        self.input_device = None
        self.output_device = None
        self.input_gain_db = 0.0
        self.threshold_db = -50.0
        self.vad_enabled = True
        self.output_volume = 1.0
        self.user_volume: dict[int, float] = {}
        self.local_mutes: set[int] = set()
        self.bitrate = 48000
        self.muted = False
        self.deafened = False
        self.loopback = False   # слышать себя (проверка микрофона)
        self.ptt_mode = False   # режим рации: передача только пока зажата клавиша
        self.ptt_pressed = False
        self._ptt_until = 0.0
        self._cue = np.zeros(0, dtype=np.float32)

        # состояние для GUI
        self.input_level_db = -120.0
        self.gate_open = False
        self.udp_ok = False
        self.last_error = ""

        # аудио
        self._in_stream = None
        self._out_stream = None
        self._in_rate = SAMPLE_RATE
        self._out_rate = SAMPLE_RATE
        self._in_buf = np.zeros(0, dtype=np.float32)
        self._out_buf = np.zeros(0, dtype=np.float32)
        self._frames_q: queue.Queue = queue.Queue(maxsize=50)
        self._gate_until = 0.0
        self._speakers: dict[int, Speaker] = {}
        self._encoder = codec.make_encoder(self.bitrate)
        self._seq = 0
        self._loop_decoder = codec.make_decoder(self._encoder.codec_id)

        # сеть
        self._sock: socket.socket | None = None
        self._addr = None
        self._crypto: VoiceCrypto | None = None
        self._send_thread: threading.Thread | None = None
        self._recv_thread: threading.Thread | None = None
        self._running = threading.Event()
        self._audio_on = False

    # ============================================================ аудио
    @property
    def audio_running(self) -> bool:
        return self._audio_on

    def start_audio(self):
        """Открыть микрофон и выход. Можно без сети — для проверки микрофона."""
        if self._audio_on:
            return
        self._audio_on = True
        self._ensure_send_thread()
        self._open_streams()

    def stop_audio(self):
        self._audio_on = False
        self._close_streams()
        self.input_level_db = -120.0
        self.gate_open = False
        if self._crypto is None:
            self._stop_send_thread()

    def restart_audio(self):
        """Переоткрыть устройства (после смены в настройках)."""
        if self._audio_on:
            self._close_streams()
            self._open_streams()

    def _open_streams(self):
        self._in_buf = np.zeros(0, dtype=np.float32)
        self._out_buf = np.zeros(0, dtype=np.float32)
        self.last_error = ""
        if sd is None:
            self.last_error = "Не найден PortAudio (sounddevice)"
            return
        try:
            self._in_rate = _pick_rate(self.input_device, "input")
            self._in_stream = sd.InputStream(
                device=self.input_device, samplerate=self._in_rate, channels=1, dtype="float32",
                blocksize=0, latency="low", callback=self._on_input)
            self._in_stream.start()
        except Exception as e:
            self.last_error = f"Микрофон: {e}"
            log.warning(self.last_error)
            self._in_stream = None
        try:
            self._out_rate = _pick_rate(self.output_device, "output")
            info = sd.query_devices(self.output_device, "output")
            ch = 2 if info["max_output_channels"] >= 2 else 1
            self._out_stream = sd.OutputStream(
                device=self.output_device, samplerate=self._out_rate, channels=ch, dtype="float32",
                blocksize=0, latency="low", callback=self._on_output)
            self._out_stream.start()
        except Exception as e:
            self.last_error = f"Выход: {e}"
            log.warning(self.last_error)
            self._out_stream = None

    def _close_streams(self):
        for st in (self._in_stream, self._out_stream):
            if st is not None:
                try:
                    st.stop()
                    st.close()
                except Exception:
                    pass
        self._in_stream = self._out_stream = None

    def _ensure_send_thread(self):
        if self._send_thread and self._send_thread.is_alive():
            return
        self._running.set()
        self._send_thread = threading.Thread(target=self._send_loop, name="voice-send", daemon=True)
        self._send_thread.start()

    def _stop_send_thread(self):
        self._running.clear()
        t, self._send_thread = self._send_thread, None
        if t and t is not threading.current_thread():
            t.join(timeout=1.0)

    # callback'и PortAudio (свой поток, держим их лёгкими)
    def _on_input(self, indata, frames, time_info, status):
        self.process_input_block(indata[:, 0])

    def _on_output(self, outdata, frames, time_info, status):
        block = self.render_output(frames)
        outdata[:] = block[:, None]

    def process_input_block(self, mono: np.ndarray):
        """Блок с микрофона на частоте устройства -> кадры по 20 мс @48 кГц в очередь."""
        self._in_buf = np.concatenate((self._in_buf, mono.astype(np.float32, copy=False)))
        need = int(round(FRAME * self._in_rate / SAMPLE_RATE))
        while len(self._in_buf) >= need:
            chunk, self._in_buf = self._in_buf[:need], self._in_buf[need:]
            frame = _resample(chunk, FRAME) if need != FRAME else chunk.copy()
            try:
                self._frames_q.put_nowait(frame)
            except queue.Full:
                pass

    def render_output(self, n: int) -> np.ndarray:
        """n сэмплов на частоте выхода."""
        per = int(round(FRAME * self._out_rate / SAMPLE_RATE))
        while len(self._out_buf) < n:
            mix = self._mix_frame()
            if per != FRAME:
                mix = _resample(mix, per)
            self._out_buf = np.concatenate((self._out_buf, mix))
        out, self._out_buf = self._out_buf[:n], self._out_buf[n:]
        return out

    def _mix_frame(self) -> np.ndarray:
        mix = np.zeros(FRAME, dtype=np.float32)
        now = time.monotonic()
        with self.lock:
            for uid, sp in list(self._speakers.items()):
                if not sp.playing and len(sp.frames) >= PREBUFFER:
                    sp.playing = True
                if not sp.playing:
                    if now - sp.last_rx > 10:
                        del self._speakers[uid]
                    continue
                if not sp.frames:
                    sp.playing = False  # опустел буфер — снова копим
                    continue
                frame = sp.frames.popleft()
                if self.deafened and uid != SELF_ID:
                    continue
                if uid in self.local_mutes:
                    continue
                vol = self.user_volume.get(uid, 1.0)
                f = frame.astype(np.float32) * (vol / 32768.0)
                if np.max(np.abs(f)) > 0.003:
                    sp.last_audible = now
                mix += f
        cue = self._cue
        if len(cue):
            n = min(FRAME, len(cue))
            mix[:n] += cue[:n]
            self._cue = cue[n:]
        mix *= self.output_volume
        np.clip(mix, -1.0, 1.0, out=mix)
        return mix

    def play_cue(self, kind: str):
        """Короткий сигнал: join / leave / mute / unmute."""
        tones = {"join": (660, 880), "leave": (880, 587), "mute": (440,), "unmute": (587,)}.get(kind, (660,))
        parts = []
        for f in tones:
            t = np.arange(int(SAMPLE_RATE * 0.09)) / SAMPLE_RATE
            env = np.minimum(1.0, np.minimum(t, t[::-1]) / 0.01)
            parts.append((0.18 * np.sin(2 * np.pi * f * t) * env).astype(np.float32))
        self._cue = np.concatenate(parts)

    # ============================================================ кодирование и отправка
    def _send_loop(self):
        last_hello = 0.0
        hello_interval = 0.5
        while self._running.is_set():
            try:
                frame = self._frames_q.get(timeout=0.1)
            except queue.Empty:
                frame = None
            now = time.monotonic()
            if self._crypto and now - last_hello > hello_interval:
                self._send_raw(bytes([KIND_HELLO]))
                last_hello = now
                hello_interval = 5.0 if self.udp_ok else 0.5
            if frame is None:
                continue
            self._process_frame(frame, now)

    def _process_frame(self, frame: np.ndarray, now: float):
        gain = 10 ** (self.input_gain_db / 20.0)
        frame = frame * gain
        np.clip(frame, -1.0, 1.0, out=frame)
        rms = float(np.sqrt(np.mean(frame * frame)) + 1e-9)
        db = 20 * math.log10(rms)
        self.input_level_db = db
        if self.ptt_mode:
            if self.ptt_pressed:
                self._ptt_until = now + 0.15  # хвост, чтобы не обрезать конец фразы
            self.gate_open = now < self._ptt_until
        else:
            if not self.vad_enabled or db >= self.threshold_db:
                self._gate_until = now + 0.4
            self.gate_open = now < self._gate_until
        transmit = self.gate_open and not self.muted
        if not transmit and not (self.loopback and self.gate_open):
            return
        pcm = (frame * 32767).astype(np.int16)
        try:
            payload = self._encoder.encode(pcm)
        except Exception as e:
            self.last_error = f"Кодек: {e}"
            return
        self._seq = (self._seq + 1) & 0xFFFF
        if self.loopback:
            self.receive_audio(SELF_ID, self._encoder.codec_id, self._seq, payload)
        if transmit and self._crypto:
            self._send_raw(bytes([KIND_AUDIO, self._encoder.codec_id]) + struct.pack(">H", self._seq) + payload)

    def _send_raw(self, plain: bytes):
        if not (self._sock and self._crypto and self._addr):
            return
        try:
            self._sock.sendto(self._crypto.seal(plain), self._addr)
        except OSError as e:
            self.last_error = f"UDP: {e}"

    # ============================================================ приём
    def _recv_loop(self, sock: socket.socket):
        while self._sock is sock:
            try:
                data, _ = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                if self._sock is not sock:
                    break
                time.sleep(0.05)  # Windows: ICMP «порт недоступен» приходит как ошибка recv
                continue
            cr = self._crypto
            if cr is None:
                continue
            plain = cr.open(data)
            if not plain:
                continue
            if plain[0] == KIND_HELLO:
                self.udp_ok = True
            elif plain[0] == KIND_AUDIO and len(plain) >= 8:
                uid = struct.unpack_from(">I", plain, 1)[0]
                codec_id = plain[5]
                seq = struct.unpack_from(">H", plain, 6)[0]
                self.receive_audio(uid, codec_id, seq, plain[8:])

    def receive_audio(self, uid: int, codec_id: int, seq: int, payload: bytes):
        with self.lock:
            sp = self._speakers.get(uid)
            if sp is None or sp.codec_id != codec_id:
                sp = Speaker(codec_id)
                if sp.decoder is None:
                    return
                self._speakers[uid] = sp
            sp.last_rx = time.monotonic()
            dec = sp.decoder
            if sp.expected is not None:
                diff = (seq - sp.expected) & 0xFFFF
                if diff >= 0x8000:
                    return  # запоздавший/повторный пакет
                if diff == 1 and codec_id == codec.CODEC_OPUS:
                    sp.frames.append(dec.decode(payload, fec=True))  # восстановили потерянный из FEC
                elif 1 <= diff <= 5:
                    for _ in range(diff):
                        sp.frames.append(dec.decode(None))
            sp.expected = (seq + 1) & 0xFFFF
            sp.frames.append(dec.decode(payload))
            while len(sp.frames) > MAX_BUFFER:
                sp.frames.popleft()

    # ============================================================ сеть
    def connect(self, host: str, port: int, ssrc: int, key: bytes):
        self.disconnect()
        info = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)
        self._addr = info[0][4]
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)  # Windows: не падать на WinError 10054
        sock.bind(("0.0.0.0", 0))
        self._crypto = VoiceCrypto(ssrc, key)
        self._sock = sock
        self.udp_ok = False
        self._recv_thread = threading.Thread(target=self._recv_loop, args=(sock,), name="voice-recv", daemon=True)
        self._recv_thread.start()
        self._ensure_send_thread()
        if not self._audio_on:
            self.start_audio()

    def disconnect(self):
        self._crypto = None
        sock, self._sock = self._sock, None
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        t, self._recv_thread = self._recv_thread, None
        if t and t is not threading.current_thread():
            t.join(timeout=1.0)
        self.udp_ok = False
        with self.lock:
            self._speakers.clear()

    def shutdown(self):
        self.disconnect()
        self.stop_audio()
        self._stop_send_thread()

    # ============================================================ для GUI
    def speaking_users(self) -> set[int]:
        now = time.monotonic()
        with self.lock:
            out = {uid for uid, sp in self._speakers.items() if now - sp.last_audible < SPEAK_HOLD and uid != SELF_ID}
        return out

    @property
    def in_voice(self) -> bool:
        return self._crypto is not None

    @property
    def self_speaking(self) -> bool:
        return self.gate_open and not self.muted and self._crypto is not None

    def set_bitrate(self, bps: int):
        self.bitrate = bps
        self._encoder.set_bitrate(bps)

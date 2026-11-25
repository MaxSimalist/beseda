"""Голосовой ретранслятор (SFU-лайт) поверх UDP.

Сервер не микширует звук: он принимает зашифрованные Opus-пакеты от говорящего
и пересылает их всем остальным в том же голосовом канале. Микширование — на клиенте.

Формат пакета клиент -> сервер:
    ssrc:u32 | counter:u64 | AEAD(plain, nonce=b"C\\0\\0\\0"+counter, aad=ssrc)
    plain: kind:u8 (0=hello/keepalive, 1=audio, 2=video)
      audio: codec:u8 | seq:u16 | payload
      video: frame:u16 | idx:u8 | cnt:u8 | key:u8 | chunk   (кадр H.264, нарезанный на части)

Формат пакета сервер -> клиент:
    counter:u64 | AEAD(plain, nonce=b"S\\0\\0\\0"+counter, aad=b"")
    plain: kind:u8 (0=hello-ack, 1=audio, 2=video)
      audio: speaker:u32 | codec:u8 | seq:u16 | payload
      video: speaker:u32 | frame:u16 | idx:u8 | cnt:u8 | key:u8 | chunk

Видео (демонстрация экрана) пересылается только тем участникам канала, которые
подписались на трансляцию (session.watch), — чтобы не грузить канал зрителей,
которым трансляция не нужна.

Шифр ChaCha20-Poly1305, ключ — свой у каждой голосовой сессии, выдаётся по
WebSocket (то есть внутри TLS).
"""
import asyncio
import logging
import os
import struct
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.exceptions import InvalidTag

log = logging.getLogger("beseda.voice")

MAX_PACKET = 1400
KIND_HELLO = 0
KIND_AUDIO = 1
KIND_VIDEO = 2


@dataclass
class VoiceSession:
    ssrc: int
    user_id: int
    channel_id: int
    key: bytes
    aead: ChaCha20Poly1305 = field(init=False)
    addr: tuple | None = None
    rx_counter: int = -1
    tx_counter: int = 0
    self_mute: bool = False
    self_deaf: bool = False
    streaming: bool = False          # идёт ли демонстрация экрана
    watch: set = field(default_factory=set)   # ssrc трансляций, которые смотрим

    def __post_init__(self):
        self.aead = ChaCha20Poly1305(self.key)

    def seal(self, plain: bytes) -> bytes:
        self.tx_counter += 1
        c = struct.pack(">Q", self.tx_counter)
        return c + self.aead.encrypt(b"S\0\0\0" + c, plain, b"")


class VoiceRelay(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport: asyncio.DatagramTransport | None = None
        self.by_ssrc: dict[int, VoiceSession] = {}
        self.by_channel: dict[int, set[int]] = {}  # channel_id -> {ssrc}

    # --- управление сессиями (вызывается из WebSocket-хаба) ---
    def open_session(self, user_id: int, channel_id: int) -> VoiceSession:
        while True:
            ssrc = struct.unpack(">I", os.urandom(4))[0]
            if ssrc and ssrc not in self.by_ssrc:
                break
        s = VoiceSession(ssrc=ssrc, user_id=user_id, channel_id=channel_id, key=ChaCha20Poly1305.generate_key())
        self.by_ssrc[ssrc] = s
        self.by_channel.setdefault(channel_id, set()).add(ssrc)
        return s

    def close_session(self, ssrc: int):
        s = self.by_ssrc.pop(ssrc, None)
        if s:
            members = self.by_channel.get(s.channel_id)
            if members:
                members.discard(ssrc)
                if not members:
                    self.by_channel.pop(s.channel_id, None)
            for o in self.by_ssrc.values():      # никто не должен смотреть ушедшую трансляцию
                o.watch.discard(ssrc)

    def close_channel(self, channel_id: int) -> list[VoiceSession]:
        out = [self.by_ssrc[x] for x in list(self.by_channel.get(channel_id, ())) if x in self.by_ssrc]
        for s in out:
            self.close_session(s.ssrc)
        return out

    # --- asyncio ---
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        if len(data) < 12 + 16 + 1 or len(data) > MAX_PACKET:
            return
        ssrc, counter = struct.unpack_from(">IQ", data, 0)
        s = self.by_ssrc.get(ssrc)
        if s is None or counter <= s.rx_counter:
            return
        try:
            plain = s.aead.decrypt(b"C\0\0\0" + data[4:12], data[12:], data[0:4])
        except InvalidTag:
            return
        s.rx_counter = counter
        if s.addr != addr:
            s.addr = addr  # клиент мог сменить порт из-за NAT — запоминаем последний

        kind = plain[0]
        if kind == KIND_HELLO:
            self._send(s, bytes([KIND_HELLO]))
            return
        if kind == KIND_VIDEO and len(plain) >= 6:
            if s.self_deaf:
                return
            s.streaming = True               # первый видеопакет = трансляция идёт
            out = bytes([KIND_VIDEO]) + struct.pack(">I", s.user_id) + plain[1:]
            for other_ssrc in self.by_channel.get(s.channel_id, ()):
                o = self.by_ssrc.get(other_ssrc)
                if o and o.addr and not o.self_deaf and ssrc in o.watch:
                    self._send(o, out)
            return
        if kind != KIND_AUDIO or len(plain) < 4 or s.self_mute:
            return
        out_plain = bytes([KIND_AUDIO]) + struct.pack(">I", s.user_id) + plain[1:]
        for other_ssrc in self.by_channel.get(s.channel_id, ()):
            if other_ssrc == ssrc:
                continue
            o = self.by_ssrc.get(other_ssrc)
            if o and o.addr and not o.self_deaf:
                self._send(o, out_plain)

    def _send(self, s: VoiceSession, plain: bytes):
        if self.transport and s.addr:
            try:
                self.transport.sendto(s.seal(plain), s.addr)
            except Exception as e:  # pragma: no cover
                log.debug("udp send failed: %s", e)

    def error_received(self, exc):  # pragma: no cover
        log.debug("udp error: %s", exc)

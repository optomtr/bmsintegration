"""Мусор из сети: разбор обязан пережить всё, не расти и не заливать журнал.

Аудит на долгую автономную работу (ноябрь 2026). За годы без присмотра соединение
увидит всё: оборванные и склеенные кадры, шлюз с прошивкой, которая отвечает
не тем JSON, устройство с огромным номером кадра. Здесь проверяются свойства,
а не случаи - тысячи случайных потоков с фиксированным зерном (падение
воспроизводится дословно):

  * add_data не бросает никогда: исключение в data_received закрывает сокет,
    а за сокетом шлюза - весь этаж;
  * буфер ограничен - иначе поток мусора съедает память;
  * подделанный кадр 3.4/3.5 не доходит до слушателя;
  * странный, но правильно подписанный JSON не превращается в ERROR с
    трассировкой на КАЖДЫЙ кадр: устройство шлёт состояние раз в секунды, и за
    год это гигабайты в home-assistant.log;
  * после любого номера кадра от устройства мы всё ещё можем собрать свой.
"""

import asyncio
import json
import logging
import os
import random
import struct
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "custom_components", "bms_integration", "core"))

import pytuya  # noqa: E402
from pytuya import parser  # noqa: E402
from pytuya.cipher import AESCipher  # noqa: E402
from pytuya.const import Affix, CMDType, MessagePayload, TuyaMessage  # noqa: E402

KEY = "0123456789abcdef"
KEY_B = KEY.encode()
DEV = "fuzzdevice0000000001"
VERSIONS = (3.3, 3.4, 3.5)
SEED = 20261111
NO_HEADER = {int(c) for c in pytuya.NO_PROTOCOL_HEADER_CMDS}


def device_frame(version: float, seqno: int, cmd: int, body: bytes) -> bytes:
    """Кадр «как от устройства»: с кодом возврата, как его шлёт железо."""
    header = str(version).encode() + b"\x00" * 12
    if body and cmd not in NO_HEADER and version >= 3.4:
        body = header + body
    if version >= 3.5:
        msg = TuyaMessage(seqno, cmd, None, struct.pack(">I", 0) + body, 0, True,
                          Affix.prefix_6699.value, True)
        return parser.pack_message(msg, hmac_key=KEY_B)
    encrypted = AESCipher(KEY_B).encrypt(body, False) if body else b""
    if encrypted and cmd not in NO_HEADER and version == 3.3:
        encrypted = header + encrypted
    msg = TuyaMessage(seqno, cmd, 0, struct.pack(">I", 0) + encrypted, 0, True,
                      Affix.prefix_55aa.value, False)
    return parser.pack_message(msg, hmac_key=KEY_B if version >= 3.4 else None)


def status_frame(version: float, seqno: int, obj) -> bytes:
    body = obj if isinstance(obj, bytes) else json.dumps(obj, separators=(",", ":")).encode()
    return device_frame(version, seqno, int(CMDType.STATUS), body)


def chunks(rng: random.Random, data: bytes, biggest: int = 97):
    at = 0
    while at < len(data):
        step = rng.randint(1, biggest)
        yield data[at : at + step]
        at += step


def dispatcher(version: float, delivered: list):
    d = pytuya.MessageDispatcher(DEV, lambda msg, ack=False: delivered.append(msg), version, KEY_B)
    d.set_logger(logging.getLogger("fuzz"), DEV)
    return d


class Listener(pytuya.TuyaListener):
    def __init__(self):
        self.sub_devices = {}
        self.statuses = []

    def status_updated(self, status):
        self.statuses.append(dict(status))

    def disconnected(self, exc=""):
        pass

    def subdevice_state_updated(self, state):
        pass


class Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def loud(self):
        return [r for r in self.records if r.levelno >= logging.ERROR]


def protocol(version: float):
    """Настоящий TuyaProtocol без сокета; вызывать внутри работающего цикла."""
    listener = Listener()
    proto = pytuya.TuyaProtocol(DEV, KEY, version, False, listener)
    proto._listener_ref = listener  # слабая ссылка внутри: держим объект живым
    return proto, listener


class RandomStreams(unittest.TestCase):
    def test_garbage_never_raises_and_the_buffer_stays_bounded(self):
        rng = random.Random(SEED)
        prefixes = (Affix.prefix_55aa.bin, Affix.prefix_6699.bin)
        ceiling = parser.MAX_PAYLOAD_BYTES + 64
        for version in VERSIONS:
            for _ in range(250):
                d = dispatcher(version, [])
                for _ in range(rng.randint(1, 40)):
                    kind = rng.random()
                    if kind < 0.35:
                        piece = rng.randbytes(rng.randint(0, 700))
                    elif kind < 0.6:
                        piece = rng.choice(prefixes) + rng.randbytes(rng.randint(0, 40))
                    elif kind < 0.8:
                        # Заголовок с выдуманной длиной, включая нулевую и огромную.
                        length = rng.choice((0, 1, 3, 4, 8, 36, 200, 15999, 16001, 2**31, 2**32 - 1))
                        piece = Affix.prefix_55aa.bin + struct.pack(">III", rng.getrandbits(32), rng.getrandbits(8), length)
                    else:
                        length = rng.choice((0, 1, 12, 28, 300, 16000, 16001, 2**32 - 1))
                        piece = Affix.prefix_6699.bin + struct.pack(">HIII", 0, rng.getrandbits(32), rng.getrandbits(8), length)
                    d.add_data(piece)  # исключение здесь = закрытый сокет шлюза
                    self.assertLessEqual(len(d.buffer), ceiling + len(piece), f"буфер растёт: {len(d.buffer)}")

    def test_a_megabyte_of_noise_leaves_three_bytes(self):
        rng = random.Random(SEED + 1)
        d = dispatcher(3.3, [])
        for _ in range(64):
            noise = bytes(b for b in rng.randbytes(16384) if b not in (0x55, 0x66))
            d.add_data(noise)
        self.assertLessEqual(len(d.buffer), 3)


class MutatedFrames(unittest.TestCase):
    def test_tampered_frames_are_never_delivered_and_never_raise(self):
        rng = random.Random(SEED + 2)
        for version in VERSIONS:
            originals = [
                status_frame(version, n, {"dps": {str(rng.randint(1, 40)): rng.choice((True, False, 7, "x" * rng.randint(0, 90)))}})
                for n in range(1, 30)
            ]
            payloads = set()
            probe = dispatcher(version, clean := [])
            for frame in originals:
                probe.add_data(frame)
            self.assertEqual(len(clean), len(originals), f"{version}: чистые кадры не дошли")
            payloads = {m.payload for m in clean}

            for _ in range(400):
                frame = bytearray(rng.choice(originals))
                for _ in range(rng.randint(1, 4)):
                    op = rng.random()
                    if op < 0.4 and frame:
                        frame[rng.randrange(len(frame))] ^= 1 << rng.randrange(8)
                    elif op < 0.6 and len(frame) > 2:
                        del frame[rng.randrange(len(frame)) :]
                    elif op < 0.8:
                        at = rng.randrange(len(frame) + 1)
                        frame[at:at] = rng.randbytes(rng.randint(1, 9))
                    else:
                        frame += rng.choice(originals)[: rng.randint(0, 60)]
                d = dispatcher(version, got := [])
                for piece in chunks(rng, bytes(frame)):
                    d.add_data(piece)
                for msg in got:
                    # Что дошло - дошло нетронутым (хвост склеенного кадра тоже может быть целым).
                    self.assertIn(msg.payload, payloads, f"{version}: до слушателя дошёл испорченный кадр")

    def test_a_good_frame_after_short_garbage_still_arrives(self):
        rng = random.Random(SEED + 3)
        for version in VERSIONS:
            for _ in range(200):
                d = dispatcher(version, got := [])
                junk = bytes(b for b in rng.randbytes(rng.randint(0, 300)) if b not in (0x55, 0x66))
                stream = junk + status_frame(version, 5, {"dps": {"1": True}})
                for piece in chunks(rng, stream, 31):
                    d.add_data(piece)
                self.assertEqual(len(got), 1, f"{version}: кадр после мусора потерян")


ODD_JSON = [
    None, 5, -1, 3.5, "x", "", [], [1, 2], True,
    {"data": None}, {"data": 5}, {"data": "dps"}, {"data": []}, {"data": {"dps": 5}},
    {"dps": 5}, {"dps": "ab"}, {"dps": [1, 2]}, {"dps": None}, {"dps": True},
    {"dps": {"1": True}, "cid": {"a": 1}}, {"dps": {"1": True}, "cid": [1]}, {"dps": {"1": True}, "cid": 7},
    {"data": {"dps": {"1": 1}, "cid": {"x": 1}}}, {"data": {"dps": "zz", "cid": "c"}},
    {"dps": {"1": {"nested": [1, {"deep": None}]}}}, {"dps": {"1": 10**40}},
    {"online": 5}, {"online": None, "offline": "x"},
]
ODD_RAW = [b"", b"{", b"}", b"nul", b"{\"dps\":", b"\xff\xfe\xfd", b"[" * 200, b"{\"dps\":{\"1\":NaN}}", b"\x00" * 64]


class OddButSignedPayloads(unittest.TestCase):
    """Кадр подписан верно, а внутри не то, чего мы ждём."""

    def run_odd(self, cmd):
        async def scenario():
            handler = Records()
            root = logging.getLogger("pytuya")
            root.addHandler(handler)
            root.setLevel(logging.DEBUG)
            try:
                for version in VERSIONS:
                    proto, listener = protocol(version)
                    seqno = 10
                    for obj in ODD_JSON:
                        seqno += 1
                        proto.data_received(device_frame(version, seqno, cmd, json.dumps(obj).encode()))
                    for raw in ODD_RAW:
                        seqno += 1
                        proto.data_received(device_frame(version, seqno, cmd, raw))
                    # И после всего этого нормальный кадр доходит как обычно.
                    proto.data_received(status_frame(version, seqno + 1, {"dps": {"9": "ok"}}))
                    self.assertTrue(listener.statuses, f"{version}: после странных кадров пропал обычный")
                    self.assertEqual(listener.statuses[-1].get("9"), "ok")
                    await proto.close()
            finally:
                root.removeHandler(handler)
            return handler

        handler = asyncio.run(scenario())
        loud = handler.loud()
        self.assertEqual(
            [], [f"{r.levelname}: {r.getMessage()[:120]}" for r in loud][:5],
            f"странный кадр даёт ERROR на каждый кадр ({len(loud)} шт.) - за год это гигабайты журнала",
        )

    def test_status_pushes_with_odd_json_are_ignored_quietly(self):
        self.run_odd(int(CMDType.STATUS))

    def test_subdevice_poll_replies_with_odd_json_are_ignored_quietly(self):
        self.run_odd(int(CMDType.LAN_EXT_STREAM))


class SequenceNumbers(unittest.TestCase):
    def test_any_device_seqno_leaves_us_able_to_send(self):
        async def scenario():
            for version in VERSIONS:
                for device_seqno in (1, 2**31 - 1, 2**31, 2**32 - 3, 2**32 - 2, 2**32 - 1):
                    proto, _listener = protocol(version)
                    proto.data_received(status_frame(version, device_seqno, {"dps": {"1": True}}))
                    for _ in range(4):
                        frame = proto._encode_message(MessagePayload(int(CMDType.HEART_BEAT), b"{}"))
                        self.assertIsInstance(frame, bytes)
                        self.assertTrue(0 <= proto.seqno <= 0xFFFFFFFF, f"{version}: seqno {proto.seqno}")
                    await proto.close()

        asyncio.run(scenario())

    def test_our_own_counter_wraps(self):
        async def scenario():
            for version in VERSIONS:
                proto, _listener = protocol(version)
                proto.seqno = 0xFFFFFFFF
                for _ in range(3):
                    proto._encode_message(MessagePayload(int(CMDType.CONTROL), b'{"dps":{"1":true}}'))
                self.assertTrue(0 <= proto.seqno <= 0xFFFFFFFF, f"{version}: seqno {proto.seqno}")
                await proto.close()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)

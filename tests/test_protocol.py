"""Запускаемые тесты исправлений протокольного слоя pytuya (Спринт 1).

Проверяют против РЕАЛЬНОГО кода репозитория:
1. TCP-фрагментация: сообщение по байту — доставляется, буфер не теряется.
2. Мусор до сообщения — ресинк и доставка.
3. Две склеенные посылки одним куском — обе доставлены немедленно.
4. Битый CRC — сообщение отброшено, диспатча нет, буфер чист.
5. Битый заголовок (огромная длина) + валидное сообщение следом — ресинк.
6. Чистый мусор гигабайтами — буфер ограничен хвостом 3 байта.
7. 6699 (v3.5, GCM): roundtrip доставляется; подделанный tag — отброшен.
8. wait_for: таймаут одной команды НЕ отменяет чужие futures (heartbeat жив).
9. _generate_payload: timestamp пишется в "t", uid не затирается.
10. Негоциация: HMAC-mismatch -> False (без отправки FINISH).
"""
import asyncio
import json
import struct
import sys
import os

import os
import sys

# Repository-relative paths so the suite runs anywhere (CI included).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.join(REPO_ROOT, "custom_components", "bms_integration")
sys.path.insert(0, os.path.join(REPO, "core"))

import pytuya  # noqa: E402
from pytuya import parser  # noqa: E402
from pytuya.const import Affix, CMDType, TuyaMessage, MessagesFormat  # noqa: E402

KEY = b"0123456789abcdef"
PASS, FAIL = [], []


def check(name, cond, details=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗ FAIL'} {name}" + (f" — {details}" if details and not cond else ""))


def make_55aa(seqno, cmd, payload_json, hmac_key=None, retcode=0):
    """Собрать сообщение «как от устройства»: retcode + payload."""
    body = struct.pack(">I", retcode) + payload_json
    msg = TuyaMessage(seqno, cmd, 0, body, 0, True, Affix.prefix_55aa.value, False)
    return parser.pack_message(msg, hmac_key=hmac_key)


async def test_dispatcher():
    print("\n== MessageDispatcher (55AA, v3.3) ==")
    delivered = []

    def cb(msg, ack=False):
        delivered.append(msg)

    d = pytuya.MessageDispatcher("deviceid123456789012", cb, 3.3, KEY)
    d.set_logger(__import__("logging").getLogger("test"), "deviceid123456789012")

    # 1. Фрагментация по байту
    fut = asyncio.get_running_loop().create_future()
    d.listeners[7] = fut
    raw = make_55aa(7, int(CMDType.DP_QUERY), b'{"dps":{"1":true}}')
    for i in range(len(raw)):
        d.add_data(raw[i : i + 1])
    check("фрагментация по 1 байту: сообщение доставлено", fut.done() and not fut.cancelled())
    if fut.done():
        check("фрагментация: payload расшифрован верно", b'"dps"' in fut.result().payload)
    check("фрагментация: буфер пуст после сборки", d.buffer == b"", repr(d.buffer))

    # 2. Мусор перед сообщением
    fut2 = asyncio.get_running_loop().create_future()
    d.listeners[8] = fut2
    raw2 = make_55aa(8, int(CMDType.DP_QUERY), b'{"dps":{"2":7}}')
    d.add_data(b"\xde\xad\xbe\xef GARBAGE \x00\x01" + raw2)
    check("мусор перед сообщением: ресинк и доставка", fut2.done())

    # 3. Две посылки одним куском
    fa = asyncio.get_running_loop().create_future()
    fb = asyncio.get_running_loop().create_future()
    d.listeners[10] = fa
    d.listeners[11] = fb
    d.add_data(
        make_55aa(10, int(CMDType.DP_QUERY), b'{"dps":{"3":1}}')
        + make_55aa(11, int(CMDType.DP_QUERY), b'{"dps":{"4":2}}')
    )
    check("склейка двух сообщений: оба доставлены сразу", fa.done() and fb.done())

    # 3b. HOL: готовое сообщение + хвост следующего (без суффикса в конце буфера)
    fc = asyncio.get_running_loop().create_future()
    d.listeners[12] = fc
    nxt = make_55aa(13, int(CMDType.DP_QUERY), b'{"dps":{"6":0}}')
    d.add_data(make_55aa(12, int(CMDType.DP_QUERY), b'{"dps":{"5":5}}') + nxt[: len(nxt) // 2])
    check("head-of-line: готовое сообщение не ждёт хвост следующего", fc.done())
    fd = asyncio.get_running_loop().create_future()
    d.listeners[13] = fd
    d.add_data(nxt[len(nxt) // 2 :])
    check("head-of-line: второй фрагмент доставлен после дозаписи", fd.done())

    # 4. Битый CRC — отбрасывается
    fut3 = asyncio.get_running_loop().create_future()
    d.listeners[20] = fut3
    bad = bytearray(make_55aa(20, int(CMDType.DP_QUERY), b'{"dps":{"9":9}}'))
    bad[20] ^= 0xFF  # порча внутри payload
    d.add_data(bytes(bad))
    check("битый CRC: сообщение отброшено (нет диспатча)", not fut3.done())
    check("битый CRC: буфер очищен, соединение живо", d.buffer == b"")
    d.listeners.pop(20, None)

    # 5. Битый заголовок (длина 60000) + валидное сообщение следом
    fut4 = asyncio.get_running_loop().create_future()
    d.listeners[21] = fut4
    fake_hdr = struct.pack(MessagesFormat.HEADER_55AA, Affix.prefix_55aa.value, 1, 10, 60000)
    d.add_data(fake_hdr + make_55aa(21, int(CMDType.DP_QUERY), b'{"dps":{"1":1}}'))
    check("битый заголовок: ресинк, валидное сообщение доставлено", fut4.done())

    # 6. Чистый мусор — буфер ограничен
    d.buffer = b""
    for _ in range(1000):
        d.add_data(os.urandom(100).replace(b"\x55", b"\x00").replace(b"\x66", b"\x00"))
    check("поток мусора: буфер ограничен хвостом <=3 байта", len(d.buffer) <= 3, f"len={len(d.buffer)}")


async def test_dispatcher_6699():
    print("\n== MessageDispatcher (6699, v3.5, GCM) ==")
    delivered = []

    def cb(msg, ack=False):
        delivered.append(msg)

    d = pytuya.MessageDispatcher("deviceid123456789012", cb, 3.5, KEY)
    d.set_logger(__import__("logging").getLogger("test"), "deviceid123456789012")

    # retcode=0 (int): реальные ответы устройств несут 4-байтовый retcode внутри шифртекста
    msg = TuyaMessage(5, int(CMDType.DP_QUERY_NEW), 0, b'{"dps":{"1":true}}', 0, True, Affix.prefix_6699.value, True)
    raw = parser.pack_message(msg, hmac_key=KEY)

    fut = asyncio.get_running_loop().create_future()
    d.listeners[5] = fut
    # фрагментация пополам
    d.add_data(raw[: len(raw) // 2])
    check("6699: половина сообщения — ждём, не теряем", not fut.done() and len(d.buffer) > 0)
    d.add_data(raw[len(raw) // 2 :])
    check("6699: сообщение собрано и доставлено", fut.done())
    if fut.done():
        check("6699: payload расшифрован", b'"dps"' in fut.result().payload, repr(fut.result().payload[:40]))

    # Подделанный GCM tag -> crc_good=False -> отброшено
    fut2 = asyncio.get_running_loop().create_future()
    d.listeners[6] = fut2
    msg2 = TuyaMessage(6, int(CMDType.DP_QUERY_NEW), 0, b'{"dps":{"2":2}}', 0, True, Affix.prefix_6699.value, True)
    raw2 = bytearray(parser.pack_message(msg2, hmac_key=KEY))
    raw2[-6] ^= 0xFF  # порча GCM tag
    d.add_data(bytes(raw2))
    check("6699: подделанный tag — нерасшифрованное НЕ диспатчится", not fut2.done())
    check("6699: буфер очищен после отброса", d.buffer == b"")


async def test_wait_for_no_cascade():
    print("\n== wait_for: таймаут не каскадирует ==")

    def cb(msg, ack=False):
        pass

    d = pytuya.MessageDispatcher("deviceid123456789012", cb, 3.3, KEY)
    d.set_logger(__import__("logging").getLogger("test"), "deviceid123456789012")
    heartbeat_future = asyncio.get_running_loop().create_future()
    d.listeners[d.HEARTBEAT_SEQNO] = heartbeat_future

    try:
        await d.wait_for(42, int(CMDType.DP_QUERY), timeout=0.05)
        check("wait_for: таймаут выброшен", False)
    except TimeoutError:
        check("wait_for: TimeoutError для своей команды", True)

    check("wait_for: чужой (heartbeat) future НЕ отменён", not heartbeat_future.cancelled())
    check("wait_for: свой listener убран", 42 not in d.listeners)

    # heartbeat future всё ещё рабочий: released -> получает результат
    d._release_listener(d.HEARTBEAT_SEQNO, "pong")
    check("wait_for: heartbeat future получает ответ после чужого таймаута", heartbeat_future.result() == "pong")


async def test_generate_payload_t():
    print("\n== _generate_payload: поле t ==")
    proto = pytuya.TuyaProtocol("deviceid123456789012", KEY.decode(), 3.3, False, pytuya.EmptyListener())
    p = proto._generate_payload(CMDType.CONTROL, {"1": True})
    data = json.loads(p.payload.decode())
    check("v3.3 CONTROL: t = строковый unix timestamp", data.get("t", "").isdigit() and int(data["t"]) > 1_700_000_000, str(data))
    check("v3.3 CONTROL: uid не затёрт timestamp'ом", data.get("uid") == "deviceid123456789012", str(data.get("uid")))

    proto34 = pytuya.TuyaProtocol("deviceid123456789012", KEY.decode(), 3.4, False, pytuya.EmptyListener())
    p34 = proto34._generate_payload(CMDType.CONTROL, {"1": True})
    data34 = json.loads(p34.payload.decode())
    check("v3.4 CONTROL: t = int timestamp", isinstance(data34.get("t"), int) and data34["t"] > 1_700_000_000, str(data34))
    check("v3.4 CONTROL: cid отсутствует для родителя", "cid" not in data34.get("data", {}), str(data34))
    # Значения DP на месте
    check("v3.4 CONTROL: dps в data", data34.get("data", {}).get("dps") == {"1": True}, str(data34))


async def test_negotiation_hmac_reject():
    print("\n== Негоциация: отказ при неверном HMAC ==")
    import hmac as hmac_mod
    from hashlib import sha256

    proto = pytuya.TuyaProtocol("deviceid123456789012", KEY.decode(), 3.4, False, pytuya.EmptyListener())
    sent = []

    async def fake_exchange_quick(payload, recv_retries):
        sent.append(payload)
        if len(sent) == 1:
            # Ответ шлюза: remote_nonce + НЕВЕРНЫЙ hmac (48 байт), зашифрованный ECB real key
            bogus = b"fedcba9876543210" + b"\x00" * 32
            from pytuya.cipher import AESCipher
            enc = AESCipher(KEY).encrypt(bogus, use_base64=False, pad=True)
            return TuyaMessage(2, int(CMDType.SESS_KEY_NEG_RESP), 0, enc, 0, True, Affix.prefix_55aa.value, False)
        return None

    proto.exchange_quick = fake_exchange_quick
    ok = await proto._negotiate_session_key()
    check("HMAC-mismatch: негоциация возвращает False", ok is False)
    check("HMAC-mismatch: FINISH не отправлен (нет 2-го сообщения)", len(sent) == 1, f"sent={len(sent)}")
    check("HMAC-mismatch: session key не подменён", proto.local_key == proto.real_local_key)


async def test_gcm_nonce_unique():
    print("\n== GCM nonce уникален ==")
    from pytuya.cipher import AESCipher
    c = AESCipher(KEY)
    ivs = set()
    for _ in range(200):
        blob = c.encrypt(b'{"x":1}', use_base64=False, pad=False, iv=True, header=b"hdr")
        ivs.add(bytes(blob[:12]))
    check("200 шифрований -> 200 уникальных nonce", len(ivs) == 200, f"уникальных={len(ivs)}")


async def main():
    await test_dispatcher()
    await test_dispatcher_6699()
    await test_wait_for_no_cascade()
    await test_generate_payload_t()
    await test_negotiation_hmac_reject()
    await test_gcm_nonce_unique()
    print(f"\n===== ИТОГ: PASS {len(PASS)} / FAIL {len(FAIL)} =====")
    if FAIL:
        print("Провалено:", *FAIL, sep="\n  - ")
        sys.exit(1)


asyncio.run(main())

"""Групповая команда Zigbee: одна передача на всю группу.

Формат восстановлен из приложения Tuya: к команде дочернему устройству
добавляются ctype = 2 и mbid - адрес группы внутри шлюза (в документации
TuyaOS - mb_id, «multicast ID»). Шлюз разворачивает это в одну передачу на
группу вместо команды каждой лампе - ради этого всё и затеяно: 25 ламп холла
на одной рации шлюза вставали в очередь и теряли команды.

Какую обёртку ждёт шлюз 3.5, из приложения не видно, поэтому проверяем обе
формы. Какая верная - решит железо; здесь фиксируем, что кадр собран ровно
так, как задумано.
"""

import asyncio
import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "custom_components", "bms_integration", "core"))

import pytuya  # noqa: E402
from pytuya import CMDType  # noqa: E402


def make_protocol():
    proto = pytuya.TuyaProtocol.__new__(pytuya.TuyaProtocol)
    proto.id = "bf739c662c423049eevo6x"
    proto.sent = []

    async def fake_exchange(command=None, dps=None, nodeID=None, payload=None):
        proto.sent.append((command, payload))
        return None

    proto.exchange = fake_exchange
    proto.debug = lambda *a, **kw: None
    return proto


def body_of(proto):
    command, payload = proto.sent[-1]
    return command, payload.cmd, json.loads(payload.payload.decode())


class TheGroupFrame(unittest.TestCase):
    def test_wrapped_form_matches_our_subdevice_control(self):
        """Обёртка протоколом 5 - как мы уже шлём лампам за шлюзом 3.5."""
        proto = make_protocol()
        asyncio.run(proto.set_group_dps({"1": True}, "a4c138fe326332aa", "8001"))

        command, cmd, body = body_of(proto)
        self.assertEqual(command, CMDType.CONTROL_NEW)
        self.assertEqual(cmd, CMDType.CONTROL_NEW)
        self.assertEqual(body["protocol"], 5)
        self.assertIsInstance(body["t"], int)
        self.assertEqual(
            body["data"],
            {"cid": "a4c138fe326332aa", "ctype": 2, "mbid": "8001", "dps": {"1": True}},
        )

    def test_flat_form_matches_the_app(self):
        """Плоская форма - так её собирает SDK приложения."""
        proto = make_protocol()
        asyncio.run(
            proto.set_group_dps({"1": False}, "a4c138fe326332aa", 8001, wrapped=False)
        )

        _, _, body = body_of(proto)
        self.assertEqual(body["cid"], "a4c138fe326332aa")
        self.assertEqual(body["ctype"], 2, "без ctype шлюз сочтёт это командой одной лампе")
        self.assertEqual(body["mbid"], "8001", "адрес группы обязан уйти строкой")
        self.assertEqual(body["dps"], {"1": False})
        self.assertEqual(body["devId"], proto.id)

    def test_the_frame_is_compact(self):
        """Как у стандартной сборки: без пробелов после разделителей."""
        proto = make_protocol()
        asyncio.run(proto.set_group_dps({"1": True}, "cid1", "1"))
        raw = proto.sent[-1][1].payload.decode()
        self.assertNotIn(": ", raw)
        self.assertNotIn(", ", raw)


class TheCloudLookup(unittest.TestCase):
    def _cloud(self, answers):
        # aiohttp и прочее ядро подставляют заглушки, как в остальных наборах.
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import ha_stubs

        ha_stubs.install()
        from custom_components.bms_integration.core.cloud_api import TuyaCloudApi

        cloud = TuyaCloudApi.__new__(TuyaCloudApi)
        cloud.asked = []

        async def fake_request(method, url, body=None, headers={}):
            cloud.asked.append(url)
            return answers.get(url)

        cloud.async_make_request = fake_request
        return cloud

    def test_every_group_of_the_device_is_detailed(self):
        cloud = self._cloud(
            {
                "/v2.0/cloud/thing/group/device/dev1": {
                    "success": True,
                    "result": [{"id": "g1"}, {"id": "g2"}],
                },
                "/v2.0/cloud/thing/group/g1": {"success": True, "result": {"name": "A"}},
                "/v2.0/cloud/thing/group/g2": {"success": True, "result": {"name": "B"}},
            }
        )
        out = asyncio.run(cloud.async_get_device_groups_raw("dev1"))
        self.assertEqual([g["group_id"] for g in out["groups"]], ["g1", "g2"])
        self.assertEqual(out["groups"][0]["detail"]["result"]["name"], "A")

    def test_a_paged_answer_is_read_too(self):
        """Облако Tuya отдаёт списки то массивом, то внутри list."""
        cloud = self._cloud(
            {
                "/v2.0/cloud/thing/group/device/dev1": {
                    "success": True,
                    "result": {"list": [{"id": "g9"}]},
                },
                "/v2.0/cloud/thing/group/g9": {"success": True, "result": {}},
            }
        )
        out = asyncio.run(cloud.async_get_device_groups_raw("dev1"))
        self.assertEqual([g["group_id"] for g in out["groups"]], ["g9"])

    def test_a_failed_lookup_is_returned_not_hidden(self):
        """Ответ облака отдаём как есть - в нём и будет видно, почему пусто."""
        cloud = self._cloud(
            {"/v2.0/cloud/thing/group/device/dev1": {"success": False, "code": 1106}}
        )
        out = asyncio.run(cloud.async_get_device_groups_raw("dev1"))
        self.assertEqual(out["groups"], [])
        self.assertEqual(out["by_device"]["code"], 1106)


if __name__ == "__main__":
    unittest.main(verbosity=2)

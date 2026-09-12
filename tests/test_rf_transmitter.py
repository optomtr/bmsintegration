"""РЧ-передатчик: частота обязана доехать до железки.

У Home Assistant два отдельных домена - `infrared` и `radio_frequency`. Я
сперва решил, что радиочастот в ядре нет вовсе, потому что проверил только
домен `infrared`; владелец прислал снимок раздела «Радиочастотные устройства»,
и это оказалось неверно. Отсюда и набор: цена ошибки здесь в том, что просьба
на 315 МГц молча уедет на 433.92.

Путь заученных кнопок достаёт частоту из самого кода. Ядро отдаёт её отдельным
полем, поэтому у передатчика свой путь отправки.
"""

import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

remote_mod = ha_stubs.load_platform("remote")


class FakeDevice:
    is_connecting = False

    def __init__(self):
        self.sent = []

    async def set_dps(self, commands):
        self.sent.append(commands)


def make_remote(enum_controller=False):
    """Тип управления - вычисляемое свойство: задаём его конфигурацией.

    ENUM получается, когда настроен отдельный датапоинт обучения.
    """
    entity = remote_mod.LocalTuyaRemote.__new__(remote_mod.LocalTuyaRemote)
    entity._dp_id = "201"
    entity._device = FakeDevice()
    conf = {remote_mod.CONF_KEY_STUDY_DP: "202"} if enum_controller else {}
    entity._config = conf
    entity.has_config = lambda key: key in conf
    entity.debug = lambda *a, **kw: None
    return entity


def payload_of(entity):
    """Разобрать то, что ушло в датапоинт."""
    sent = entity._device.sent[-1]
    return json.loads(sent["201"])


def send(entity, code="AAAA", freq=433_920_000, repeats=6):
    asyncio.run(entity.async_send_rf_raw(code, freq, repeats))


class TheFrequencyReachesTheHardware(unittest.TestCase):
    def test_433_is_sent_as_megahertz(self):
        entity = make_remote()
        send(entity, freq=433_920_000)
        self.assertEqual(payload_of(entity)["study_feq"], "433.92")

    def test_315_is_not_silently_turned_into_433(self):
        """Ровно та ошибка, ради которой этот путь и написан."""
        entity = make_remote()
        send(entity, freq=315_000_000)
        self.assertEqual(payload_of(entity)["study_feq"], "315")

    def test_the_code_travels_in_key1(self):
        entity = make_remote()
        send(entity, code="Zm9v")
        self.assertEqual(payload_of(entity)["key1"]["code"], "Zm9v")

    def test_the_repeat_count_is_passed_on(self):
        entity = make_remote()
        send(entity, repeats=3)
        self.assertEqual(payload_of(entity)["key1"]["times"], "3")

    def test_it_is_marked_as_an_rf_send(self):
        entity = make_remote()
        send(entity)
        body = payload_of(entity)
        self.assertEqual(body["control"], "rfstudy_send")
        self.assertEqual(body["rf_type"], "sub_2g")

    def test_an_enum_controller_says_so_instead_of_sending_nonsense(self):
        """Такой передатчик сырую посылку не примет - молчать об этом нельзя."""
        entity = make_remote(enum_controller=True)
        with self.assertRaises(Exception):
            send(entity)
        self.assertEqual(entity._device.sent, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

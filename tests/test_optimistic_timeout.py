"""Неполученное подтверждение - не отказ.

Владелец с объекта: «свет фактически выключается, статус на Home Assistant
неверный, у хаба проблем нет». Так и оказалось. Занятый Zigbee-шлюз команду
выполняет, а подтверждение присылает с опозданием или не присылает вовсе.
Откат оптимистичного значения в этот момент превращал удавшееся действие в
неверный показ - и поправить показ было нечему: устройство ведь изменилось,
просто промолчало, а значит своего обновления не пришлёт.

Настоящий отказ - обрыв, закрытая сессия - по-прежнему обязан откатываться.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

ha_stubs.load_sensor()   # любая платформа тянет за собой entity.py
base = sys.modules["custom_components.bms_integration.entity"]


class FakeDevice:
    is_connecting = False
    is_sleep = False
    connected = True

    def __init__(self, fail_with=None):
        self.fail_with = fail_with
        self.rollbacks = []
        self.verifies = 0
        self.sent = []

    async def set_dps(self, status):
        self.sent.append(status)
        if self.fail_with:
            raise self.fail_with

    def restore_optimistic_status(self, applied, previous):
        self.rollbacks.append((applied, previous))

    def schedule_status_verify(self):
        self.verifies += 1


def make_entity(fail_with=None):
    ent = base.LocalTuyaEntity.__new__(base.LocalTuyaEntity)
    ent._device = FakeDevice(fail_with)
    ent.warnings = []
    ent.warning = lambda m, *a: ent.warnings.append(str(m))
    ent.debug = lambda *a, **kw: None
    return ent


def send(ent, status=None):
    asyncio.run(
        base.LocalTuyaEntity._send_dps_background(
            ent, status or {"1": False}, {"1": False}, {"1": True}
        )
    )


class ATimeoutIsNotARefusal(unittest.TestCase):
    def test_a_timeout_does_not_roll_the_state_back(self):
        """Ровно случай с объекта: свет погас, показ обязан это удержать."""
        ent = make_entity(TimeoutError("Command 13 timed out waiting for sequence 57242"))
        send(ent)

        self.assertEqual(
            ent._device.rollbacks, [],
            "откат вернул «включено» лампе, которая физически погасла",
        )

    def test_a_timeout_asks_the_device_for_the_truth(self):
        """Держать ожидаемое значение можно только вместе со сверкой."""
        ent = make_entity(TimeoutError("timed out"))
        send(ent)

        self.assertEqual(ent._device.verifies, 1, "состояние не пошли перепроверять")

    def test_a_real_refusal_still_rolls_back(self):
        """Обрыв - устройство правда не изменилось, поправить нас нечему."""
        ent = make_entity(ConnectionResetError("the session was closed"))
        send(ent)

        self.assertEqual(len(ent._device.rollbacks), 1, "настоящий отказ не откатили")
        self.assertEqual(ent._device.verifies, 0)

    def test_a_successful_command_touches_nothing(self):
        ent = make_entity(None)
        send(ent)

        self.assertEqual(ent._device.rollbacks, [])
        self.assertEqual(ent._device.verifies, 0)
        self.assertEqual(ent._device.sent, [{"1": False}])


if __name__ == "__main__":
    unittest.main(verbosity=2)

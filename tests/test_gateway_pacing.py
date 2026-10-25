"""Команды шлюзу - по очереди, в темпе самого шлюза.

С объекта (HA Green, 90 Zigbee-устройств за шлюзами Tuya): «Алиса, выключи
свет» - и часть ламп остаётся гореть. Голосовая команда - это десятки
выключений в одну секунду; интеграция отправляла их пачкой, шлюз не успевал и
терял часть. Раньше на другом объекте проход по 36 лампам дал 297 таймаутов.

Теперь следующая команда дочернему устройству уходит, когда шлюз подтвердил
приём предыдущей, но не чаще CommandPacer.gap. Опоздавшее подтверждение не
держит очередь дольше ack_wait. Устройства со своим сокетом (Wi-Fi) очередь не
ждут.

Время здесь настоящее, но сжатое: интервал 20 мс, ожидание ответа 100 мс.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

coordinator = ha_stubs.load_coordinator()
pytuya = sys.modules["custom_components.bms_integration.core.pytuya"]

GAP, ACK_WAIT = 0.02, 0.1


class Gateway:
    """Общий сокет шлюза: записывает, когда что пришло, и отвечает с задержкой."""

    def __init__(self, ack=0.005, fail=()):
        self.command_pacer = pytuya.CommandPacer(GAP, ACK_WAIT)
        self.ack = ack
        self.fail = set(fail)
        self.sent = []  # (время, cid, dps)
        self.in_flight = 0
        self.max_in_flight = 0
        self.is_connected = True

    async def set_dps(self, dps, cid=None):
        loop = asyncio.get_running_loop()
        self.sent.append((loop.time(), cid, dict(dps)))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            ack = self.ack(cid) if callable(self.ack) else self.ack
            await asyncio.sleep(ack)
            if cid in self.fail:
                raise TimeoutError(f"Command 7 timed out ({cid})")
        finally:
            self.in_flight -= 1


def device(iface, cid):
    dev = coordinator.TuyaDevice.__new__(coordinator.TuyaDevice)
    dev._interface = iface
    dev._node_id = cid
    dev._pending_status = {}
    dev._command_failures = 0
    dev.warning = lambda *a, **kw: None

    async def check_connection():
        return None

    async def handle_failure(ex):
        return None

    dev.check_connection = check_connection
    dev._async_handle_command_failure = handle_failure
    return dev


def burst(iface, commands):
    """Все команды разом, как от голосового помощника; вернуть исходы."""

    async def run():
        return await asyncio.gather(
            *(dev.set_dps(dps) for dev, dps in commands), return_exceptions=True
        )

    return asyncio.run(run())


class VoiceCommandBurst(unittest.TestCase):
    def test_forty_lights_go_one_by_one_with_the_gap(self):
        gw = Gateway()
        lights = [device(gw, f"cid{i}") for i in range(40)]
        burst(gw, [(d, {"1": False}) for d in lights])
        self.assertEqual(len(gw.sent), 40, "команды потерялись в очереди")
        self.assertEqual(sorted(c for _, c, _ in gw.sent), sorted(d._node_id for d in lights))
        self.assertEqual(gw.max_in_flight, 1, "шлюзу ушло несколько команд разом")
        gaps = [b[0] - a[0] for a, b in zip(gw.sent, gw.sent[1:])]
        self.assertGreaterEqual(min(gaps), GAP * 0.9, f"слишком часто: {min(gaps):.3f} с")

    def test_the_gateway_sets_the_pace(self):
        # Медленный шлюз отвечает за 60 мс - очередь ждёт ответа, а не гонит.
        gw = Gateway(ack=0.06)
        burst(gw, [(device(gw, f"cid{i}"), {"1": False}) for i in range(5)])
        gaps = [b[0] - a[0] for a, b in zip(gw.sent, gw.sent[1:])]
        self.assertGreaterEqual(min(gaps), 0.055)
        self.assertEqual(gw.max_in_flight, 1)

    def test_a_late_answer_does_not_hold_the_house(self):
        # Одна лампа не отвечает 3 секунды: остальные уходят через ack_wait,
        # а не после её таймаута.
        gw = Gateway(ack=lambda cid: 3.0 if cid == "stuck" else 0.005)
        stuck = device(gw, "stuck")
        others = [device(gw, f"cid{i}") for i in range(3)]

        async def run():
            loop = asyncio.get_running_loop()
            start = loop.time()
            task = asyncio.ensure_future(stuck.set_dps({"1": False}))
            await asyncio.sleep(0)
            await asyncio.gather(*(d.set_dps({"1": False}) for d in others))
            done = loop.time() - start
            task.cancel()
            return done

        done = asyncio.run(run())
        self.assertEqual(len(gw.sent), 4)
        self.assertLess(done, 1.0, f"очередь ждала зависшую лампу {done:.2f} с")

    def test_a_failed_command_releases_the_queue_and_still_fails(self):
        gw = Gateway(fail={"bad"})
        bad, good = device(gw, "bad"), device(gw, "good")

        async def run():
            loop = asyncio.get_running_loop()
            start = loop.time()
            results = await asyncio.gather(
                bad.set_dps({"1": False}), good.set_dps({"1": False}), return_exceptions=True
            )
            return results, loop.time() - start

        (bad_result, good_result), took = asyncio.run(run())
        # Отказ доходит до вызывающего - иначе не откатить показ в интерфейсе.
        self.assertIsInstance(bad_result, TimeoutError)
        self.assertIsNone(good_result)
        self.assertLess(took, ACK_WAIT, "после отказа очередь ждала полное ack_wait")

    def test_channels_of_one_device_merge_while_waiting(self):
        # Выключатель на три канала, каналы приходят с разбросом в 10 мс, пока
        # шлюз занят соседом: в очереди они копятся и уходят одной передачей.
        gw = Gateway(ack=lambda cid: 0.06 if cid == "cid0" else 0.005)
        first = device(gw, "cid0")
        triple = device(gw, "cid1")

        async def channel(dp, delay):
            await asyncio.sleep(delay)
            await triple.set_dps({dp: False})

        async def run():
            await asyncio.gather(
                first.set_dps({"1": False}),
                channel("1", 0.005), channel("2", 0.015), channel("3", 0.025),
            )

        asyncio.run(run())
        to_triple = [dps for _, cid, dps in gw.sent if cid == "cid1"]
        self.assertEqual(to_triple, [{"1": False, "2": False, "3": False}])

    def test_wifi_devices_do_not_wait(self):
        # Своё соединение у каждого: очередь шлюза их не касается.
        gw = Gateway(ack=0.03)
        plugs = [device(gw, None) for _ in range(5)]
        burst(gw, [(d, {"1": False}) for d in plugs])
        self.assertEqual(len(gw.sent), 5)
        self.assertGreater(gw.max_in_flight, 1, "Wi-Fi-устройства встали в очередь шлюза")

    def test_each_gateway_has_its_own_queue(self):
        # Две очереди не ждут друг друга: два шлюза работают параллельно.
        gw1, gw2 = Gateway(ack=0.05), Gateway(ack=0.05)

        async def run():
            loop = asyncio.get_running_loop()
            start = loop.time()
            await asyncio.gather(
                *(device(gw1, f"a{i}").set_dps({"1": False}) for i in range(4)),
                *(device(gw2, f"b{i}").set_dps({"1": False}) for i in range(4)),
            )
            return loop.time() - start

        took = asyncio.run(run())
        self.assertLess(took, 0.35, f"шлюзы ждали друг друга: {took:.2f} с")

    def test_the_real_protocol_has_a_queue(self):
        # Очередь своя у каждого соединения - то есть у каждого шлюза.
        import inspect

        self.assertIn("self.command_pacer = CommandPacer()", inspect.getsource(pytuya.TuyaProtocol.__init__))
        self.assertGreater(pytuya.SUBDEVICE_COMMAND_GAP, 0)
        self.assertLessEqual(pytuya.SUBDEVICE_COMMAND_GAP, 0.1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

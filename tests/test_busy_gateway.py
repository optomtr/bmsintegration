"""Занятый шлюз - не сломанный шлюз.

Счётчик неудачных команд у каждого устройства свой, а сокет у всех общий: по
правилу «сокет сбрасывает только шлюз» три неполученных ответа у одной лампы
рвали соединение всем семидесяти. При резком проходе по комнате таймауты
копятся мгновенно - и сброс убивает все команды, которые в этом сокете летят,
порождая новые неудачи. Сброс вызывает сброс.

Замер на объекте: один резкий проход по 36 лампам дал 297 таймаутов, 88
закрытых сессий и 69 переподключений, а свет остался гореть.

Живой шлюз отличается от мёртвого свежим обменом: пока он разгребает очередь,
соседям он отвечает. Мёртвый молчит всем.
"""

import asyncio
import errno
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

coordinator = ha_stubs.load_coordinator()


class Sibling:
    def __init__(self, last):
        self._last_successful_update_time = last


def make_child(gateway_last=None, siblings=(), own_last=None):
    """Ребёнок шлюза: сокет общий, счётчик неудач свой."""
    gw = coordinator.TuyaDevice.__new__(coordinator.TuyaDevice)
    gw._last_successful_update_time = gateway_last
    gw.sub_devices = {f"cid{i}": s for i, s in enumerate(siblings)}

    dev = coordinator.TuyaDevice.__new__(coordinator.TuyaDevice)
    dev.gateway = gw
    dev.sub_devices = {}
    dev._last_successful_update_time = own_last
    dev._command_failures = coordinator.COMMAND_FAILURES_BEFORE_RESET - 1
    dev.resets = []
    dev.debug = lambda *a, **kw: None

    async def reset(reason, event):
        dev.resets.append(reason)

    dev._async_reset_stale_connection = reset

    class Iface:
        is_connected = True

    dev._interface = Iface()
    return dev, gw


def fail_once(dev, ex=None):
    """Ещё одна неудачная команда - та, что добирает счётчик до порога."""
    asyncio.run(
        coordinator.TuyaDevice._async_handle_command_failure(
            dev, ex or TimeoutError("Command 13 timed out waiting for sequence 57242")
        )
    )


class BusyIsNotBroken(unittest.TestCase):
    def test_a_busy_gateway_keeps_its_socket(self):
        """Ровно случай с объекта: очередь, а не поломка."""
        now = time.monotonic()
        dev, gw = make_child(siblings=[Sibling(now - 1.0)])
        fail_once(dev)

        self.assertEqual(dev.resets, [], "у занятого шлюза оторвали общий сокет")

    def test_a_neighbours_traffic_counts_as_proof_of_life(self):
        """Наши ответы стоят в очереди, а соседям шлюз отвечает."""
        now = time.monotonic()
        dev, gw = make_child(siblings=[Sibling(None), Sibling(now - 2.0)])
        fail_once(dev)

        self.assertEqual(dev.resets, [])

    def test_a_silent_gateway_is_still_reset(self):
        """Если по сокету давно ничего не шло - он правда мёртв."""
        old = time.monotonic() - coordinator.TRANSPORT_ALIVE_WINDOW - 5
        dev, gw = make_child(gateway_last=old, siblings=[Sibling(old)])
        fail_once(dev)

        self.assertEqual(len(dev.resets), 1, "мёртвый сокет перестали пересоздавать")

    def test_a_gateway_that_never_spoke_is_reset(self):
        dev, gw = make_child(gateway_last=None, siblings=[Sibling(None)])
        fail_once(dev)

        self.assertEqual(len(dev.resets), 1)

    def test_a_broken_socket_is_still_reset_at_once(self):
        """Оборванный сокет - не очередь, ждать нечего."""
        now = time.monotonic()
        dev, gw = make_child(siblings=[Sibling(now)])
        fail_once(dev, ConnectionResetError(errno.ECONNRESET, "reset by peer"))

        self.assertEqual(len(dev.resets), 1, "настоящий обрыв перестали лечить")

    def test_the_counter_is_cleared_so_a_busy_spell_does_not_accumulate(self):
        """Иначе следующая же неудача через час порвёт сокет."""
        now = time.monotonic()
        dev, gw = make_child(siblings=[Sibling(now)])
        fail_once(dev)

        self.assertEqual(dev._command_failures, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

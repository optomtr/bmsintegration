"""Зонд канарейки: что интеграция показала на этом ядре Home Assistant.

Ставится только на стенд tools/core_canary.py. После запуска ядра ждёт, пока
устройства подключатся, переключает одну лампу через обычную службу (команда
проходит весь путь до симулятора и обратно) и пишет итог в canary.json - без
входа в Home Assistant и без сети.
"""

import asyncio
import json

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.helpers import entity_registry as er

DOMAIN = "canary_probe"
TARGET = "bms_integration"
SETTLE_SECONDS = 25


async def async_setup(hass, config):
    async def _report(_event=None):
        await asyncio.sleep(SETTLE_SECONDS)
        registry = er.async_get(hass)
        entities = [e for e in registry.entities.values() if e.platform == TARGET]
        states = {
            e.entity_id: (state.state if (state := hass.states.get(e.entity_id)) else None)
            for e in entities
        }
        result = {
            "entries": {
                entry.title: str(entry.state).rsplit(".", 1)[-1].lower()
                for entry in hass.config_entries.async_entries(TARGET)
            },
            "states": states,
            "command": None,
        }
        light = next(
            (eid for eid, st in sorted(states.items()) if eid.startswith("light.") and st in ("on", "off")),
            None,
        )
        if light:
            before = states[light]
            service = "turn_off" if before == "on" else "turn_on"
            try:
                await hass.services.async_call("light", service, {"entity_id": light}, blocking=True)
                await asyncio.sleep(3)
                after = hass.states.get(light).state
                result["command"] = {"entity": light, "before": before, "after": after, "ok": after != before}
            except Exception as ex:  # noqa: BLE001 - итог пишем в любом случае
                result["command"] = {"entity": light, "before": before, "error": repr(ex), "ok": False}

        def _write():
            with open(hass.config.path("canary.json"), "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False, indent=1)

        await hass.async_add_executor_job(_write)

    # Слушатель - сама сопрограмма: обычную функцию без @callback ядро
    # выполняет в потоке, а создавать задачи оттуда нельзя.
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _report)
    return True

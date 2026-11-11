#!/usr/bin/env python3
"""Канарейка: интеграция на НАСТОЯЩЕМ ядре Home Assistant, без железа.

Наборы в tests/ идут поверх заглушек и не видят главного риска долгой жизни:
ядро меняется каждый месяц. В ноябре 2026 оно само сообщило, что четыре
вызова интеграции перестанут работать в 2027 году, - и узнать об этом можно
было только запустив её на свежем ядре и прочитав журнал.

Скрипт поднимает симулятор шлюза с устройствами и настоящий Home Assistant из
указанного окружения, ждёт, пока интеграция загрузится, устройства получат
состояние, а команда дойдёт до устройства и вернётся, - и читает журнал:
ошибки интеграции и предупреждения ядра «устарело / перестанет работать».

    python3 tools/core_canary.py --hass ~/.cache/ha-2026.11/bin/hass
    python3 tools/core_canary.py --hass "$(which hass)" --port 8123

Код возврата: 0 - чисто; 1 - интеграция не работает на этом ядре; 2 -
работает, но ядро объявило что-то из используемого устаревшим (или в журнале
ошибки интеграции). CI запускает это по расписанию на свежем выпуске ядра и на
бета-версии следующего: о поломке узнаём за месяцы, а не от объекта.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))

SIM = os.path.join(REPO, "tools", "tuya_device_sim.py")
PROTOCOL = "3.5"
DEPRECATION = re.compile(r"deprecat|will stop working|will be removed|breaks in", re.I)


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def devices() -> dict:
    """Шлюз симулятора и его дочерние устройства в виде записи конфигурации."""
    out = subprocess.run(
        [sys.executable, SIM, "--scenario"], capture_output=True, text=True, check=True
    ).stdout
    config = json.loads(out)
    keep = {}
    for dev_id, dev in config.items():
        # Один адрес - одно соединение: шлюз и всё, что за ним.
        if dev.get("gateway_id") or any(d.get("gateway_id") == dev_id for d in config.values()):
            keep[dev_id] = {**dev, "protocol_version": PROTOCOL}
    return keep


def build_config(root: str, port: int) -> None:
    # Порт не по умолчанию задаётся в YAML. Ядро 2026.9+ принимает такую
    # настройку «на пробу» и через 5 минут без подтверждения откатывает её -
    # канарейка укладывается в это время с запасом.
    http = f"http:\n  server_port: {port}\n" if port != 8123 else ""
    write(
        os.path.join(root, "configuration.yaml"),
        "homeassistant:\n  name: Canary\n  time_zone: UTC\n" + http + "canary_probe:\n",
    )
    components = os.path.join(root, "custom_components")
    os.makedirs(components)
    shutil.copytree(
        os.path.join(REPO, "custom_components", "bms_integration"),
        os.path.join(components, "bms_integration"),
        ignore=shutil.ignore_patterns("__pycache__", ".claude-flow"),
    )
    shutil.copytree(os.path.join(REPO, "tools", "canary_probe"), os.path.join(components, "canary_probe"))
    # Запись интеграции кладём в хранилище до первого запуска ядра (ядро
    # остановлено - писать можно): мастер настройки проверяет устройства по
    # одному и для автомата не годится. Формат - самый старый, который ядро
    # умеет поднять; до текущего оно доводит его своими миграциями.
    write(
        os.path.join(root, ".storage", "core.config_entries"),
        json.dumps({
            "version": 1, "minor_version": 1, "key": "core.config_entries",
            "data": {"entries": [{
                "entry_id": "canary0000000000000000000000001", "version": 4,
                "domain": "bms_integration", "title": "canary",
                "data": {"region": "eu", "client_id": "", "client_secret": "", "user_id": "",
                         "username": "canary", "no_cloud": True, "devices": devices()},
                "options": {}, "pref_disable_new_entities": False, "pref_disable_polling": False,
                "source": "user", "unique_id": None, "disabled_by": None,
            }]},
        }),
    )


def read_log(root: str) -> tuple[list[str], list[str]]:
    """(устаревшее, ошибки) - строки журнала, относящиеся к интеграции."""
    path = os.path.join(root, "home-assistant.log")
    deprecated, errors = [], []
    if not os.path.exists(path):
        return deprecated, ["журнал Home Assistant не создан"]
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    for index, line in enumerate(lines):
        ours = "bms_integration" in line
        if ours and DEPRECATION.search(line):
            deprecated.append(line)
        elif ours and (" ERROR " in line or "Error setting up" in line or "Error adding entit" in line):
            errors.append(line)
        elif line.startswith("Traceback") and any(
            "custom_components/bms_integration" in follow for follow in lines[index : index + 60]
        ):
            errors.append("Traceback с кодом интеграции: " + next(
                (f.strip() for f in lines[index : index + 60] if "custom_components/bms_integration" in f), ""))
    return deprecated, errors


def stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=40)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hass", default=shutil.which("hass") or "hass", help="исполняемый файл hass нужного ядра")
    ap.add_argument("--port", type=int, default=8123, help="порт веб-интерфейса стенда")
    ap.add_argument("--timeout", type=int, default=420, help="сколько секунд ждать итога (первый запуск ставит зависимости)")
    ap.add_argument("--keep", action="store_true", help="не удалять каталог стенда")
    args = ap.parse_args()

    root = tempfile.mkdtemp(prefix="bms-canary-")
    sim = hass = None
    try:
        build_config(root, args.port)
        version = subprocess.run([args.hass, "--version"], capture_output=True, text=True).stdout.strip()
        print(f"Home Assistant {version}; стенд {root}")
        sim = subprocess.Popen(
            [sys.executable, SIM, "--host", "127.0.0.1", "--port", "6668", "--protocol", PROTOCOL],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(1.5)
        if sim.poll() is not None:
            print("✗ симулятор не запустился: порт 6668 занят?")
            return 1
        with open(os.path.join(root, "hass.out"), "w", encoding="utf-8") as out:
            # cwd - каталог стенда: запущенное из корня репозитория ядро на
            # части сборок Python находит custom_components в текущем каталоге
            # раньше, чем в своём, и берёт интеграцию не из стенда, а зонд не
            # находит вовсе.
            hass = subprocess.Popen(
                [args.hass, "-c", root], stdout=out, stderr=subprocess.STDOUT, cwd=root
            )
            result_path = os.path.join(root, "canary.json")
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline and not os.path.exists(result_path):
                if hass.poll() is not None:
                    print(f"✗ Home Assistant завершился с кодом {hass.returncode}")
                    break
                time.sleep(2)
            time.sleep(2)
        stop(hass)
        hass = None

        deprecated, errors = read_log(root)
        failed = False
        if not os.path.exists(os.path.join(root, "canary.json")):
            print("✗ итог не получен: ядро не запустилось или интеграция его уронила")
            failed = True
            result = {"entries": {}, "states": {}, "command": None}
        else:
            with open(os.path.join(root, "canary.json"), encoding="utf-8") as fh:
                result = json.load(fh)

        entries = result["entries"]
        if entries.get("canary") != "loaded":
            print(f"✗ запись интеграции не загружена: {entries}")
            failed = True
        else:
            print("✓ запись интеграции загружена")
        states = result["states"]
        alive = {e: s for e, s in states.items() if s not in (None, "unavailable", "unknown")}
        if len(alive) < 8:
            print(f"✗ живых сущностей {len(alive)} из {len(states)}: {states}")
            failed = True
        else:
            print(f"✓ сущностей с состоянием: {len(alive)} из {len(states)}")
        command = result.get("command") or {}
        if not command.get("ok"):
            print(f"✗ команда не дошла до устройства и обратно: {command}")
            failed = True
        else:
            print(f"✓ команда прошла: {command['entity']} {command['before']} → {command['after']}")

        for line in errors:
            print("✗ ошибка в журнале:", line[:400])
        for line in deprecated:
            print("! ядро объявило устаревшим:", line[:600])
        if failed:
            return 1
        if errors or deprecated:
            return 2
        print("✓ в журнале нет ни ошибок интеграции, ни объявлений об устаревшем")
        return 0
    finally:
        stop(hass)
        stop(sim)
        if args.keep:
            print("стенд оставлен:", root)
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Тест скрипта переноса с оригинального LocalTuya (tools/migrate_from_localtuya.py).

Строит синтетический .storage Home Assistant с записью LocalTuya и посторонней
интеграцией, прогоняет миграцию и проверяет, что домен переписан, данные
устройств и entity_id сохранены, а чужая интеграция не затронута.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "tools", "migrate_from_localtuya.py")

PASS, FAIL = [], []


OK = "\u2713"
BAD = "\u2717 FAIL"


def check(name, cond, details=""):
    (PASS if cond else FAIL).append(name)
    mark = OK if cond else BAD
    suffix = f" - {details}" if details and not cond else ""
    print(f"  {mark} {name}{suffix}")


def build_storage(storage):
    def w(name, payload):
        with open(os.path.join(storage, name), "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    w("core.config_entries", {"version": 1, "key": "core.config_entries", "data": {"entries": [
        {"entry_id": "E1", "version": 4, "domain": "localtuya", "title": "LocalTuya", "data": {
            "no_cloud": True,
            "devices": {
                "bfGATE": {"host": "192.168.1.60", "local_key": "k1", "protocol_version": "3.4", "entities": []},
                "bfSUB": {"host": "192.168.1.60", "local_key": "k1", "node_id": "a4c138aa",
                          "gateway_id": "bfGATE", "protocol_version": "3.4",
                          "entities": [{"id": "1", "platform": "binary_sensor"}]},
            }}, "options": {}, "source": "user"},
        {"entry_id": "E2", "version": 1, "domain": "hue", "title": "Hue", "data": {}, "options": {}, "source": "user"},
    ]}})
    w("core.device_registry", {"version": 1, "key": "core.device_registry", "data": {"devices": [
        {"id": "D1", "identifiers": [["localtuya", "local_bfGATE"]], "config_entries": ["E1"]},
        {"id": "D2", "identifiers": [["hue", "abc"]], "config_entries": ["E2"]},
    ], "deleted_devices": []}})
    w("core.entity_registry", {"version": 1, "key": "core.entity_registry", "data": {"entities": [
        {"entity_id": "binary_sensor.door", "platform": "localtuya",
         "unique_id": "local_bfSUB_1", "config_entry_id": "E1", "device_id": "D1"},
        {"entity_id": "light.hue", "platform": "hue", "unique_id": "h1",
         "config_entry_id": "E2", "device_id": "D2"},
    ], "deleted_entities": [{"entity_id": "switch.gone", "platform": "localtuya", "unique_id": "local_bfX_1"}]}})
    w("localtuya_remotes_codes", {"version": 1, "key": "localtuya_remotes_codes",
                                  "data": {"bfIR": {"TV": {"power": "CODE"}}}})


def run(config_dir, *args):
    return subprocess.run([sys.executable, SCRIPT, config_dir, *args],
                          capture_output=True, text=True)


def main():
    print("== Перенос с оригинального LocalTuya ==")
    tmp = tempfile.mkdtemp()
    try:
        storage = os.path.join(tmp, ".storage")
        os.makedirs(storage)
        build_storage(storage)

        # 1. Dry run ничего не пишет
        before = open(os.path.join(storage, "core.config_entries"), encoding="utf-8").read()
        res = run(tmp)
        check("dry run завершается успешно", res.returncode == 0, res.stderr)
        check("dry run сообщает о найденной записи", "domain bms_integration" in res.stdout)
        after = open(os.path.join(storage, "core.config_entries"), encoding="utf-8").read()
        check("dry run НЕ изменяет файлы", before == after)

        # 2. Apply
        res = run(tmp, "--apply")
        check("apply завершается успешно", res.returncode == 0, res.stderr)

        ce = json.load(open(os.path.join(storage, "core.config_entries"), encoding="utf-8"))
        dr = json.load(open(os.path.join(storage, "core.device_registry"), encoding="utf-8"))
        er = json.load(open(os.path.join(storage, "core.entity_registry"), encoding="utf-8"))

        e1 = [x for x in ce["data"]["entries"] if x["entry_id"] == "E1"][0]
        e2 = [x for x in ce["data"]["entries"] if x["entry_id"] == "E2"][0]
        check("домен записи переписан", e1["domain"] == "bms_integration")
        check("версия записи не изменена", e1["version"] == 4)
        check("устройства и ключи сохранены", e1["data"]["devices"]["bfGATE"]["local_key"] == "k1")
        check("node_id саб-устройства сохранён", e1["data"]["devices"]["bfSUB"]["node_id"] == "a4c138aa")
        check("чужая интеграция не тронута", e2["domain"] == "hue")

        d1 = [x for x in dr["data"]["devices"] if x["id"] == "D1"][0]
        d2 = [x for x in dr["data"]["devices"] if x["id"] == "D2"][0]
        check("identifiers устройства переписаны", d1["identifiers"] == [["bms_integration", "local_bfGATE"]])
        check("чужое устройство не тронуто", d2["identifiers"] == [["hue", "abc"]])

        ents = {x["entity_id"]: x for x in er["data"]["entities"]}
        check("platform сущности переписан", ents["binary_sensor.door"]["platform"] == "bms_integration")
        check("entity_id сохранён", "binary_sensor.door" in ents)
        check("unique_id сохранён", ents["binary_sensor.door"]["unique_id"] == "local_bfSUB_1")
        check("чужая сущность не тронута", ents["light.hue"]["platform"] == "hue")
        check("удалённые сущности переведены",
              er["data"]["deleted_entities"][0]["platform"] == "bms_integration")

        check("ИК-коды переименованы", os.path.exists(os.path.join(storage, "bms_integration_remotes_codes")))
        check("старый файл ИК-кодов убран", not os.path.exists(os.path.join(storage, "localtuya_remotes_codes")))
        rc = json.load(open(os.path.join(storage, "bms_integration_remotes_codes"), encoding="utf-8"))
        check("ключ внутри файла обновлён", rc["key"] == "bms_integration_remotes_codes")
        check("сами коды сохранены", rc["data"]["bfIR"]["TV"]["power"] == "CODE")
        check("созданы резервные копии", os.path.exists(os.path.join(storage, "core.config_entries.bak")))


        # 4. Запись новее, чем понимает форк -> блокировка, файлы не тронуты
        tmp2 = tempfile.mkdtemp()
        try:
            st2 = os.path.join(tmp2, ".storage")
            os.makedirs(st2)
            build_storage(st2)
            ce_path = os.path.join(st2, "core.config_entries")
            data = json.load(open(ce_path, encoding="utf-8"))
            data["data"]["entries"][0]["version"] = 99
            json.dump(data, open(ce_path, "w", encoding="utf-8"))
            before2 = open(ce_path, encoding="utf-8").read()
            res = run(tmp2, "--apply")
            check("запись новее форка: возврат != 0", res.returncode != 0)
            check("запись новее форка: сообщение о блокировке", "BLOCKED" in res.stdout)
            check("запись новее форка: файлы не изменены",
                  open(ce_path, encoding="utf-8").read() == before2)
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

        # 5. --optimistic off фиксирует прежнее поведение
        tmp3 = tempfile.mkdtemp()
        try:
            st3 = os.path.join(tmp3, ".storage")
            os.makedirs(st3)
            build_storage(st3)
            # шаблон пользователя + служебный sample
            tpl_src = os.path.join(tmp3, "custom_components", "localtuya", "templates")
            tpl_dst = os.path.join(tmp3, "custom_components", "bms_integration", "templates")
            os.makedirs(tpl_src)
            os.makedirs(tpl_dst)
            open(os.path.join(tpl_src, "my_house.yaml"), "w").write("x: 1\n")
            open(os.path.join(tpl_src, "sample_skip.yaml"), "w").write("x: 1\n")

            res = run(tmp3, "--apply", "--optimistic", "off")
            check("--optimistic off отрабатывает", res.returncode == 0, res.stderr)
            entry = json.load(open(os.path.join(st3, "core.config_entries"), encoding="utf-8"))
            ents = entry["data"]["entries"][0]["data"]["devices"]["bfSUB"]["entities"]
            check("--optimistic off: опция записана явно",
                  all(e.get("optimistic") is False for e in ents))
            check("пользовательский шаблон перенесён",
                  os.path.exists(os.path.join(tpl_dst, "my_house.yaml")))
            check("служебный sample_* не переносится",
                  not os.path.exists(os.path.join(tpl_dst, "sample_skip.yaml")))
        finally:
            shutil.rmtree(tmp3, ignore_errors=True)

        # 3. Повторный запуск безопасен
        res = run(tmp, "--apply")
        check("повторный запуск не ломает ничего", res.returncode == 0 and "nothing to do" in res.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n===== ИТОГ: PASS {len(PASS)} / FAIL {len(FAIL)} =====")
    if FAIL:
        print("Провалено:", *FAIL, sep="\n  - ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

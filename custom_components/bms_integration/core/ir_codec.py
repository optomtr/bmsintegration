"""Перевод между длительностями импульсов и кодами Tuya.

Home Assistant в новой платформе `infrared` даёт команду как СЫРЫЕ
длительности в микросекундах плюс несущую частоту. Передатчик Tuya такого не
принимает: он работает со своим кодом - массивом 16-битных длительностей,
сжатым по схеме FastLZ и завёрнутым в base64. Этот модуль переводит одно в
другое в обе стороны.

Формат разобран по сохранённым кодам самого устройства: числа идут парами
«импульс - пауза», порядок байтов младшим вперёд, значения в микросекундах.
Сжатие - FastLZ первого уровня: устройство принимает и сжатый код, и
несжатый, а присылает при обучении сжатый.
"""

from __future__ import annotations

import base64

# Длительность хранится в двух байтах, поэтому всё, что длиннее, обрезается.
# Настоящих пауз такой длины в ИК-посылках не бывает: даже межкадровый разрыв
# укладывается в десятки миллисекунд.
MAX_DURATION_US = 0xFFFF


def pulses_to_bytes(pulses: list[int]) -> bytes:
    """Сложить длительности в поток 16-битных чисел, младший байт вперёд."""
    out = bytearray()
    for pulse in pulses:
        value = min(abs(int(pulse)), MAX_DURATION_US)
        out += value.to_bytes(2, "little")
    return bytes(out)


def bytes_to_pulses(raw: bytes) -> list[int]:
    """Разобрать поток обратно в длительности."""
    if len(raw) % 2:
        raise ValueError("Нечётная длина потока: это не пары байтов")
    return [
        int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)
    ]


def fastlz_decompress(data: bytes) -> bytes:
    """Распаковать FastLZ первого уровня.

    Свой разбор нужен потому, что готовой реализации в зависимостях нет, а
    тянуть ради этого целую библиотеку в интеграцию не хочется.
    """
    out = bytearray()
    pos = 0
    end = len(data)
    while pos < end:
        ctrl = data[pos]
        pos += 1
        if ctrl < 32:
            # Литералы: столько байтов копируется как есть.
            count = ctrl + 1
            if pos + count > end:
                raise ValueError("Обрыв на литералах")
            out += data[pos : pos + count]
            pos += count
            continue
        # Ссылка назад: длина в старших битах, смещение в остальных.
        length = ctrl >> 5
        ref_hi = (ctrl & 0x1F) << 8
        if length == 7:
            if pos >= end:
                raise ValueError("Обрыв на длинной ссылке")
            length += data[pos]
            pos += 1
        if pos >= end:
            raise ValueError("Обрыв на смещении")
        offset = ref_hi | data[pos]
        pos += 1
        start = len(out) - offset - 1
        if start < 0:
            raise ValueError("Ссылка за начало данных")
        for _ in range(length + 2):
            out.append(out[start])
            start += 1
    return bytes(out)


def fastlz_compress(data: bytes) -> bytes:
    """Сжать по FastLZ первого уровня.

    Реализация намеренно простая: ищем повтор перебором в пределах окна.
    Посылка редко длиннее нескольких сотен байт, поэтому скорость здесь
    значения не имеет, а читаемость имеет.
    """
    out = bytearray()
    literals = bytearray()
    pos = 0
    end = len(data)

    def flush_literals() -> None:
        # Литералы пишутся кусками не длиннее 32 байт - столько вмещает
        # управляющий байт.
        start = 0
        while start < len(literals):
            chunk = literals[start : start + 32]
            out.append(len(chunk) - 1)
            # extend, а не "+=": внутри замыкания составное присваивание
            # сделало бы out локальной переменной и функция падала бы сразу.
            out.extend(chunk)
            start += 32
        literals.clear()

    while pos < end:
        best_len = 0
        best_off = 0
        window_start = max(0, pos - 0x1FFF)
        for candidate in range(window_start, pos):
            length = 0
            while (
                pos + length < end
                and length < 264
                and data[candidate + length] == data[pos + length]
            ):
                length += 1
            if length > best_len:
                best_len, best_off = length, pos - candidate - 1
        if best_len >= 3:
            flush_literals()
            length = best_len - 2
            if length < 7:
                out.append((length << 5) | (best_off >> 8))
            else:
                out.append((7 << 5) | (best_off >> 8))
                out.append(length - 7)
            out.append(best_off & 0xFF)
            pos += best_len
        else:
            literals.append(data[pos])
            pos += 1
    flush_literals()
    return bytes(out)


def pulses_to_tuya(pulses: list[int], compress: bool = True) -> str:
    """Длительности -> код Tuya в base64."""
    raw = pulses_to_bytes(pulses)
    payload = fastlz_compress(raw) if compress else raw
    return base64.b64encode(payload).decode()


def tuya_to_pulses(code: str) -> list[int]:
    """Код Tuya -> длительности.

    Устройство присылает сжатый код, но встречаются и несжатые. Сначала
    пробуем распаковать; если вышла бессмыслица - читаем как есть.
    """
    raw = base64.b64decode(code)
    try:
        unpacked = fastlz_decompress(raw)
    except ValueError:
        unpacked = raw
    else:
        if len(unpacked) % 2:
            unpacked = raw
    return bytes_to_pulses(unpacked)

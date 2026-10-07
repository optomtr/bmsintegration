"""Поправка цвета RGB-ленты по замеру камерой телефона.

Контроллер получает оттенок и насыщенность и сам раскладывает их на красный,
зелёный и синий каналы. Светодиоды и прошивки у контроллеров разные: у одной
ленты белый синеет, у другой оранжевый выходит красным. Камера телефона с
зафиксированным балансом белого измеряет, какой свет даёт каждый канал, и
отсюда считается, какую команду послать, чтобы лента показала выбранный цвет.

Модель ленты: свет = M · (d ** γ). Здесь d - доли каналов 0..1, которые
контроллер выводит из команды (обычное HSV -> RGB), M - вклад каждого канала в
цвет, увиденный камерой, γ - нелинейность канала. Камера с балансом белого на
дневной свет (6500 K, или заморожена по белому экрану) видит цвет почти в
линейном sRGB, поэтому цель для выбранного в Home Assistant цвета - его
линейный sRGB, а для белого - дневной белый.

Замер устойчив к автоэкспозиции телефона: берутся только соотношения каналов
внутри каждого кадра, а не яркость между кадрами. Яркость каналов друг
относительно друга выводится из белого: он складывается из трёх каналов.

Модуль без зависимостей от Home Assistant - его проверяют тесты.
"""

from __future__ import annotations

import colorsys
import math
from dataclasses import dataclass

# Базовый замер: что показать и как назвать. Порядок - порядок показа.
SAMPLES: tuple[tuple[str, tuple[float, float], str], ...] = (
    ("red", (0, 100), "Красный"),
    ("green", (120, 100), "Зелёный"),
    ("blue", (240, 100), "Синий"),
    ("white", (0, 0), "Белый из цветов"),
    # Половинная насыщенность: два канала на половине - отсюда нелинейность.
    ("red_soft", (0, 50), "Светло-красный"),
    ("green_soft", (120, 50), "Светло-зелёный"),
    ("blue_soft", (240, 50), "Светло-синий"),
)

# Собственный белый режим лампы. У адресных лент (GLEDOPTO SPI) его собирает
# из тех же трёх цветов прошивка, и поправка цвета его не касается: если он
# заметно не дневной, белый пойдёт через исправленный цвет.
WHITE_MODE: tuple[str, None, str] = ("white_mode", None, "Белый режим лампы")
WHITE_MODE_TOLERANCE = 0.01

# Проверка после поправки. Насыщенность не до края: край sRGB камера
# телефона обрезает, и проверка мерила бы камеру, а не ленту.
CHECKS: tuple[tuple[str, tuple[float, float], str], ...] = (
    ("white", (0, 0), "Белый"),
    ("orange", (30, 85), "Оранжевый"),
    ("green", (120, 70), "Зелёный"),
    ("sky", (200, 70), "Голубой"),
    ("pink", (330, 50), "Розовый"),
)

GAMMA_MIN, GAMMA_MAX = 0.4, 3.0
MIN_SPREAD = 0.06

# sRGB (D65) -> XYZ: для оценки ошибки в равноконтрастных координатах u'v'.
_SRGB_TO_XYZ = (
    (0.4124, 0.3576, 0.1805),
    (0.2126, 0.7152, 0.0722),
    (0.0193, 0.1192, 0.9505),
)


class CalibrationError(ValueError):
    """Замер не годится. Текст - для человека, по-русски."""


# --------------------------------------------------------------------------- #
# Цветовые преобразования
# --------------------------------------------------------------------------- #
def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(c: float) -> float:
    c = max(0.0, c)
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def hs_to_rgb(hs) -> list[float]:
    """Оттенок 0-360 и насыщенность 0-100 -> RGB 0..1 при полной яркости."""
    return list(colorsys.hsv_to_rgb((float(hs[0]) % 360) / 360, float(hs[1]) / 100, 1.0))


def rgb_to_hs(rgb) -> tuple[float, float]:
    h, s, _v = colorsys.rgb_to_hsv(*rgb)
    return (round(h * 360, 2) % 360, round(s * 100, 2))


def uv_prime(rgb_linear) -> tuple[float, float]:
    """Линейный sRGB -> u'v' (CIE 1976); масштаб значения не имеет."""
    x, y, z = (sum(row[i] * rgb_linear[i] for i in range(3)) for row in _SRGB_TO_XYZ)
    d = x + 15 * y + 3 * z
    if d <= 0:
        raise CalibrationError("В кадре нет света - камера не видит ленту.")
    return (4 * x / d, 9 * y / d)


def delta_uv(a_linear, b_linear) -> float:
    ua, va = uv_prime(a_linear)
    ub, vb = uv_prime(b_linear)
    return math.hypot(ua - ub, va - vb)


def words_for(delta: float) -> str:
    """Насколько заметна разница цвета - словами."""
    if delta < 0.005:
        return "на глаз не отличить"
    if delta < 0.012:
        return "едва заметно"
    if delta < 0.025:
        return "заметно"
    return "сильно"


D65_WHITE = (1.0, 1.0, 1.0)


# --------------------------------------------------------------------------- #
# 3×3
# --------------------------------------------------------------------------- #
def _det(m) -> float:
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )


def _inv(m):
    det = _det(m)
    if abs(det) < 1e-9:
        raise CalibrationError(
            "Каналы ленты в кадре неразличимы - камера видит не ленту, "
            "а посторонний свет."
        )
    return [
        [
            (m[(j + 1) % 3][(i + 1) % 3] * m[(j + 2) % 3][(i + 2) % 3]
             - m[(j + 1) % 3][(i + 2) % 3] * m[(j + 2) % 3][(i + 1) % 3]) / det
            for j in range(3)
        ]
        for i in range(3)
    ]


def _mul(m, v) -> list[float]:
    return [sum(m[i][j] * v[j] for j in range(3)) for i in range(3)]


def _columns(*cols):
    return [[cols[j][i] for j in range(3)] for i in range(3)]


def _unit(v) -> list[float]:
    total = sum(v)
    if total <= 0:
        raise CalibrationError("В кадре нет света - камера не видит ленту.")
    return [x / total for x in v]


def _spread(*colours) -> float:
    """Площадь треугольника цветов на плоскости цветности r-g."""
    (x1, y1), (x2, y2), (x3, y3) = ((c[0], c[1]) for c in colours)
    return abs((x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1)) / 2


def _clean(sample, name: str) -> list[float]:
    try:
        values = [float(x) for x in sample]
    except (TypeError, ValueError):
        raise CalibrationError(f"Замер «{name}» повреждён.") from None
    if len(values) != 3 or not all(math.isfinite(x) and x >= 0 for x in values):
        raise CalibrationError(f"Замер «{name}» повреждён.")
    return _unit(values)


# --------------------------------------------------------------------------- #
# Поправка
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Calibration:
    """Модель одной ленты: вклад каналов (по столбцам) и их нелинейность."""

    matrix: tuple[tuple[float, float, float], ...]
    gamma: tuple[float, float, float]
    # Белый (насыщенность 0) - через исправленный цвет, а не белым режимом.
    white_via_colour: bool = False

    def __post_init__(self):
        self._inverse()  # необратимая модель не может быть поправкой

    def _inverse(self):
        return _inv(self.matrix)

    # Команда, которую надо послать, чтобы лента показала hs.
    def to_device(self, hs) -> tuple[float, float]:
        target = [srgb_to_linear(c) for c in hs_to_rgb(hs)]
        power = [max(0.0, x) for x in _mul(self._inverse(), target)]
        top = max(power)
        if top <= 0:
            return (float(hs[0]), float(hs[1]))
        drive = [(p / top) ** (1 / g) for p, g in zip(power, self.gamma)]
        return rgb_to_hs(drive)

    # Что покажет лента на команду hs - в координатах выбора цвета.
    def from_device(self, hs) -> tuple[float, float]:
        drive = hs_to_rgb(hs)
        light = [max(0.0, x) for x in _mul(self.matrix, [d ** g for d, g in zip(drive, self.gamma)])]
        top = max(light)
        if top <= 0:
            return (float(hs[0]), float(hs[1]))
        encoded = [min(1.0, linear_to_srgb(x / top)) for x in light]
        result = rgb_to_hs(encoded)
        if result[1] < 0.5:
            # У белого оттенка нет: остаток насыщенности - округление команды.
            return (0.0, 0.0)
        return result

    def predict(self, hs) -> list[float]:
        """Линейный свет на команду hs (для проверок и уточнения)."""
        drive = hs_to_rgb(hs)
        return _mul(self.matrix, [d ** g for d, g in zip(drive, self.gamma)])

    def as_dict(self) -> dict:
        return {
            "version": 1,
            "matrix": [[round(x, 6) for x in row] for row in self.matrix],
            "gamma": [round(g, 4) for g in self.gamma],
            "white_via_colour": self.white_via_colour,
        }

    @classmethod
    def from_dict(cls, data) -> "Calibration":
        """Разобрать сохранённую поправку; испорченная - ошибка, не догадка."""
        if not isinstance(data, dict) or data.get("version") != 1:
            raise CalibrationError("Неизвестный формат калибровки.")
        try:
            matrix = tuple(tuple(float(x) for x in row) for row in data["matrix"])
            gamma = tuple(float(g) for g in data["gamma"])
        except (KeyError, TypeError, ValueError):
            raise CalibrationError("Калибровка повреждена.") from None
        if len(matrix) != 3 or any(len(r) != 3 for r in matrix) or len(gamma) != 3:
            raise CalibrationError("Калибровка повреждена.")
        if not all(math.isfinite(x) for r in matrix for x in r) or not all(
            math.isfinite(g) and GAMMA_MIN <= g <= GAMMA_MAX for g in gamma
        ):
            raise CalibrationError("Калибровка повреждена.")
        return cls(matrix, gamma, bool(data.get("white_via_colour", False)))

    @classmethod
    def load(cls, data) -> "Calibration | None":
        """Поправка из настроек сущности или None, если её нет или она негодна."""
        if not data:
            return None
        try:
            return cls.from_dict(data)
        except CalibrationError:
            return None


def fit(samples: dict) -> tuple[Calibration, dict]:
    """Посчитать поправку по базовому замеру.

    samples: имя из SAMPLES -> линейное среднее [r, g, b] по кадру.
    """
    missing = [name for name, *_ in SAMPLES if name not in samples]
    if missing:
        raise CalibrationError("Замер неполный - повторите калибровку.")
    unit = {name: _clean(samples[name], label) for name, _hs, label in SAMPLES}

    # Посторонний свет ложится на каждый кадр одинаково и тянет все цвета к
    # своему: красный, зелёный и синий ленты в кадре сливаются. У чистого
    # замера треугольник трёх цветов на плоскости цветности - 0,35-0,5; когда
    # свет в комнате ярче ленты, он сжимается до сотых.
    if _spread(unit["red"], unit["green"], unit["blue"]) < MIN_SPREAD:
        raise CalibrationError(
            "Камера видит в основном посторонний свет, а не ленту. Выключите "
            "другой свет, задёрните шторы и поднесите телефон ближе к стене."
        )

    primaries = _columns(unit["red"], unit["green"], unit["blue"])
    weights = _mul(_inv(primaries), unit["white"])
    if min(weights) <= 0:
        raise CalibrationError(
            "Белый не складывается из трёх цветов ленты: в кадр попадает "
            "посторонний свет или он пересвечен. Выключите другой свет и "
            "отодвиньте телефон."
        )
    matrix = _columns(*(
        [weights[c] * unit[name][i] for i in range(3)]
        for c, name in enumerate(("red", "green", "blue"))
    ))

    # Нелинейность: на половинной насыщенности два канала идут на половине.
    # По соотношению каналов в кадре видно, сколько света даёт эта половина.
    inverse = _inv(matrix)
    levels: dict[int, list[float]] = {0: [], 1: [], 2: []}
    for full, name in enumerate(("red_soft", "green_soft", "blue_soft")):
        power = _mul(inverse, unit[name])
        if power[full] <= 0:
            raise CalibrationError("Замер смешанных цветов не удался - повторите.")
        for ch in range(3):
            if ch != full:
                levels[ch].append(power[ch] / power[full])
    gamma = []
    for ch in range(3):
        level = sum(levels[ch]) / len(levels[ch])
        if not 0.0 < level < 1.2:
            raise CalibrationError("Замер смешанных цветов не удался - повторите.")
        level = min(max(level, 0.03), 0.97)
        gamma.append(min(max(math.log(level) / math.log(0.5), GAMMA_MIN), GAMMA_MAX))

    white_mode = None
    if WHITE_MODE[0] in samples:
        white_mode = delta_uv(_clean(samples[WHITE_MODE[0]], WHITE_MODE[2]), D65_WHITE)
    calibration = Calibration(
        tuple(tuple(r) for r in matrix),
        tuple(gamma),
        white_via_colour=white_mode is not None and white_mode > WHITE_MODE_TOLERANCE,
    )

    # Модель обязана объяснять сам замер: иначе он испорчен (свет мигал,
    # телефон сдвинули) и поправка по нему навредит.
    misfit = max(
        delta_uv(calibration.predict(hs), unit[name]) for name, hs, _label in SAMPLES
    )
    if misfit > 0.03:
        raise CalibrationError(
            "Замер неустойчив: цвета в кадре менялись. Держите телефон "
            "неподвижно и повторите."
        )
    # «До калибровки» - тот белый, что человек видел: белый режим лампы,
    # если он у неё есть, иначе белый из трёх цветов.
    before = white_mode if white_mode is not None else delta_uv(unit["white"], D65_WHITE)
    return calibration, {"misfit": round(misfit, 4), "white_before": round(before, 4)}


def refine_white(calibration: Calibration, measured) -> Calibration:
    """Уточнить вклад каналов по замеру исправленного белого.

    Камера видит каналы ленты на краю своей палитры не вполне точно, и белый
    по первой модели может уйти на волосок. Белый - главное, что видно, поэтому
    он замыкается по факту: что модель ждала от команды и что пришло.
    """
    seen = _clean(measured, "белый")
    command = calibration.to_device((0, 0))
    expected = [d ** g for d, g in zip(hs_to_rgb(command), calibration.gamma)]
    actual = _mul(calibration._inverse(), seen)
    if min(actual) <= 0 or min(expected) <= 0:
        raise CalibrationError("Замер белого не удался - повторите.")
    gains = [a / e for a, e in zip(actual, expected)]
    mean = sum(gains) / 3
    gains = [g / mean for g in gains]
    matrix = tuple(
        tuple(calibration.matrix[i][j] * gains[j] for j in range(3)) for i in range(3)
    )
    return Calibration(matrix, calibration.gamma, calibration.white_via_colour)


def evaluate(measured: dict) -> dict:
    """Насколько исправленные цвета совпали с целью."""
    results = []
    for key, hs, label in CHECKS:
        if key not in measured:
            continue
        seen = _clean(measured[key], label)
        target = [srgb_to_linear(c) for c in hs_to_rgb(hs)]
        delta = delta_uv(seen, target)
        results.append(
            {"key": key, "label": label, "delta": round(delta, 4), "words": words_for(delta)}
        )
    if not results:
        raise CalibrationError("Проверка не прошла - повторите калибровку.")
    deltas = [r["delta"] for r in results]
    mean = sum(deltas) / len(deltas)
    worst = max(deltas)
    if mean < 0.008 and worst < 0.015:
        verdict, text = "excellent", "Отлично: цвета совпадают с выбранными."
    elif mean < 0.015 and worst < 0.03:
        verdict, text = "good", "Хорошо: разница едва заметна."
    elif mean < 0.03:
        verdict, text = "fair", "Удовлетворительно: разница заметна. Лучше повторить в тёмной комнате."
    else:
        verdict, text = "poor", "Плохо: замер испорчен посторонним светом. Повторите в тёмной комнате."
    return {
        "results": results,
        "mean": round(mean, 4),
        "max": round(worst, 4),
        "verdict": verdict,
        "verdict_words": text,
    }

"""Поправка цвета RGB-ленты по замеру камерой телефона.

Контроллер получает оттенок и насыщенность и сам раскладывает их на красный,
зелёный и синий каналы. Светодиоды и прошивки у контроллеров разные: у одной
ленты белый синеет, у другой оранжевый выходит красным. Камера телефона с
зафиксированным балансом белого измеряет, какой свет даёт каждый канал, и
отсюда считается, какую команду послать, чтобы лента показала выбранный цвет.

Модель ленты: свет = M · e(d). Здесь d - доли каналов 0..1, которые контроллер
выводит из команды (обычное HSV -> RGB), M - вклад каждого канала в цвет,
увиденный камерой, e - кривая канала: сколько света даёт его доля. Кривая
строится по точкам из замера, а не одной степенью: на живой GLEDOPTO SPI
белый вышел точно, а жёлтые и песочные - нет, им нужны каналы на 70-90%, где
одна степень, снятая на половине, ошибалась.

Камера с балансом белого на дневной свет видит цвет почти в линейном sRGB,
поэтому цель для цвета с колеса - его линейный sRGB, для температуры -
белый чёрного тела этой температуры. Человек может подстроить белый на глаз:
подстройка ложится на все цвета и температуры одинаково.

Замер устойчив к автоэкспозиции телефона: берутся только соотношения каналов
внутри каждого кадра. Модуль без зависимостей от Home Assistant.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .colour_math import (
    DEFAULT_KELVIN,
    DEFAULT_TINT,
    ColourError,
    delta_uv,
    hs_target,
    hs_to_rgb,
    kelvin_target,
    linear_to_srgb,
    normalised,
    rgb_to_hs,
)

# Базовый замер: ключ, команда (оттенок, насыщенность), подпись. Порядок -
# порядок показа. Насыщенность 25/50/75% у чистого цвета ставит два других
# канала на 75/50/25% - три точки кривой каждого канала; тёплые и жёлтые
# оттенки дают точки там, где на них жаловались.
SAMPLES: tuple[tuple[str, tuple[float, float], str], ...] = (
    ("red", (0, 100), "Красный"),
    ("green", (120, 100), "Зелёный"),
    ("blue", (240, 100), "Синий"),
    ("white", (0, 0), "Белый из цветов"),
    ("red_25", (0, 25), "Красный, слабо"),
    ("red_50", (0, 50), "Красный, наполовину"),
    ("red_75", (0, 75), "Красный, сильно"),
    ("green_25", (120, 25), "Зелёный, слабо"),
    ("green_50", (120, 50), "Зелёный, наполовину"),
    ("green_75", (120, 75), "Зелёный, сильно"),
    ("blue_25", (240, 25), "Синий, слабо"),
    ("blue_50", (240, 50), "Синий, наполовину"),
    ("blue_75", (240, 75), "Синий, сильно"),
    ("yellow", (60, 100), "Жёлтый"),
    ("amber", (45, 100), "Янтарный"),
    ("orange", (30, 100), "Оранжевый"),
    ("sand", (38, 40), "Песочный"),
    ("cream", (45, 20), "Кремовый"),
    ("pale_sky", (210, 30), "Бледно-голубой"),
)

# Собственный белый режим лампы. У RGB-ленты без белых светодиодов (GLEDOPTO
# SPI) его собирает прошивка из тех же трёх цветов - мимо поправки.
WHITE_MODE: tuple[str, None, str] = ("white_mode", None, "Белый режим лампы")
WHITE_MODE_TOLERANCE = 0.01   # белый режим заметно не дневной
SAME_AS_RGB_TOLERANCE = 0.006  # белый режим - тот же свет, что три цвета вместе

# Проверка после поправки: ключ, цвет с колеса или температура, подпись.
# Насыщенность не до края: край sRGB камера телефона обрезает.
CHECKS: tuple[tuple[str, tuple[float, float] | None, int | None, str], ...] = (
    ("white", (0, 0), None, "Белый"),
    ("warm_3000", None, 3000, "Тёплый белый 3000 K"),
    ("sand", (38, 40), None, "Песочный"),
    ("yellow", (52, 75), None, "Жёлтый"),
    ("orange", (30, 85), None, "Оранжевый"),
    ("green", (120, 70), None, "Зелёный"),
    ("sky", (200, 70), None, "Голубой"),
    ("pink", (330, 50), None, "Розовый"),
)

WHITE_RANGE = {
    "kelvin": {"min": 2700, "max": 9000, "step": 50},
    "tint": {"min": -0.012, "max": 0.018, "step": 0.001},
}
WHITE_DEFAULT = {"kelvin": DEFAULT_KELVIN, "tint": DEFAULT_TINT}

MIN_SPREAD = 0.06
MAX_MISFIT = 0.02
# Белый ленты на стене дальше этого от дневного - подозрительно: обычно это
# не лента, а цветная поверхность (дерево, бежевая стена), которая окрашивает
# свет, и поправка подогнала бы ленту под её цвет.
STRONG_CORRECTION = 0.04
_LEVEL_MIN, _LEVEL_MAX = 0.002, 0.998
_MAX_KNOTS = 24


class CalibrationError(ValueError):
    """Замер не годится. Текст - для человека, по-русски."""


def words_for(delta: float) -> str:
    """Насколько заметна разница цвета - словами."""
    if delta < 0.005:
        return "на глаз не отличить"
    if delta < 0.012:
        return "едва заметно"
    if delta < 0.025:
        return "заметно"
    return "сильно"


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
            "Каналы ленты в кадре неразличимы - камера видит не ленту, а посторонний свет."
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
    total = sum(values)
    if total <= 0:
        raise CalibrationError("В кадре нет света - камера не видит ленту.")
    return [x / total for x in values]


def _is_dark(sample) -> bool:
    """Кадр без света: экран калибровки шлёт нули, если темно."""
    try:
        return sum(float(x) for x in sample) <= 0
    except (TypeError, ValueError):
        return False


def _delta(a, b) -> float:
    try:
        return delta_uv(a, b)
    except ColourError:
        raise CalibrationError("В кадре нет света - камера не видит ленту.") from None


# --------------------------------------------------------------------------- #
# Кривая канала
# --------------------------------------------------------------------------- #
def _monotone(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Сгладить точки так, чтобы свет рос с долей канала (pool adjacent violators)."""
    blocks: list[list[float]] = []  # [сумма d, сумма e, число]
    for d, e in sorted(points):
        blocks.append([d, e, 1])
        while len(blocks) > 1 and blocks[-2][1] / blocks[-2][2] >= blocks[-1][1] / blocks[-1][2]:
            d2, e2, n2 = blocks.pop()
            blocks[-1][0] += d2
            blocks[-1][1] += e2
            blocks[-1][2] += n2
    return [(d / n, e / n) for d, e, n in blocks]


def _level(knots, d: float) -> float:
    """Свет канала при доле d: степенные отрезки между точками кривой."""
    if d <= 0:
        return 0.0
    if d >= 1:
        return 1.0
    pts = list(knots) + [(1.0, 1.0)]
    d0, e0 = pts[0]
    if d <= d0:
        return e0 * (d / d0) ** (math.log(e0) / math.log(d0))
    for (da, ea), (db, eb) in zip(pts, pts[1:]):
        if d <= db:
            k = math.log(eb / ea) / math.log(db / da)
            return ea * (d / da) ** k
    return 1.0


def _share(knots, e: float) -> float:
    """Обратная кривая: какая доля канала даст свет e."""
    if e <= 0:
        return 0.0
    if e >= 1:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if _level(knots, mid) < e:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _gamma_knots(gamma: float):
    return tuple((d, d ** gamma) for d in (0.25, 0.5, 0.75))


# --------------------------------------------------------------------------- #
# Поправка
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Calibration:
    """Модель одной ленты: вклад каналов (по столбцам) и кривые каналов."""

    matrix: tuple[tuple[float, float, float], ...]
    curves: tuple[tuple[tuple[float, float], ...], ...]
    # Белый и температуры - через исправленный цвет, а не белым режимом лампы.
    white_via_colour: bool = False
    # Белый, подстроенный человеком на глаз; по умолчанию - дневной D65.
    white_kelvin: float = DEFAULT_KELVIN
    white_tint: float = DEFAULT_TINT

    def __post_init__(self):
        self._inverse()  # необратимая модель не может быть поправкой

    def _inverse(self):
        return _inv(self.matrix)

    def _gain(self) -> list[float]:
        """Подстройка белого: во сколько раз сдвинуть каждый канал цели."""
        user = kelvin_target(self.white_kelvin, self.white_tint)
        base = kelvin_target(DEFAULT_KELVIN, DEFAULT_TINT)
        return normalised([u / b for u, b in zip(user, base)])

    def _light(self, drive) -> list[float]:
        return _mul(self.matrix, [_level(k, d) for k, d in zip(self.curves, drive)])

    def _command(self, target) -> tuple[float, float]:
        target = [t * g for t, g in zip(target, self._gain())]
        power = [max(0.0, x) for x in _mul(self._inverse(), target)]
        top = max(power)
        if top <= 0:
            return (0.0, 0.0)
        return rgb_to_hs([_share(k, p / top) for k, p in zip(self.curves, power)])

    # Команда, которую надо послать, чтобы лента показала цвет с колеса.
    def to_device(self, hs) -> tuple[float, float]:
        return self._command(hs_target(hs))

    # Команда для белого заданной температуры.
    def to_device_kelvin(self, kelvin: float) -> tuple[float, float]:
        return self._command(kelvin_target(kelvin))

    # Что покажет лента на команду hs - в координатах выбора цвета.
    def from_device(self, hs) -> tuple[float, float]:
        light = [max(0.0, x / g) for x, g in zip(self._light(hs_to_rgb(hs)), self._gain())]
        top = max(light)
        if top <= 0:
            return (float(hs[0]), float(hs[1]))
        result = rgb_to_hs([min(1.0, linear_to_srgb(x / top)) for x in light])
        if result[1] < 0.5:
            # У белого оттенка нет: остаток насыщенности - округление команды.
            return (0.0, 0.0)
        return result

    def predict(self, hs) -> list[float]:
        """Линейный свет на команду hs (для проверок и уточнения)."""
        return self._light(hs_to_rgb(hs))

    def target(self, hs=None, kelvin=None) -> list[float]:
        """Что должна показать лента: с подстройкой белого человеком."""
        base = kelvin_target(kelvin) if kelvin is not None else hs_target(hs)
        return [t * g for t, g in zip(base, self._gain())]

    def with_white(self, kelvin: float, tint: float) -> "Calibration":
        return Calibration(self.matrix, self.curves, self.white_via_colour, kelvin, tint)

    def as_dict(self) -> dict:
        return {
            "version": 2,
            "matrix": [[round(x, 6) for x in row] for row in self.matrix],
            "curves": [[[round(d, 4), round(e, 5)] for d, e in k] for k in self.curves],
            "white_via_colour": self.white_via_colour,
            "white": {"kelvin": round(self.white_kelvin), "tint": round(self.white_tint, 4)},
        }

    @classmethod
    def from_dict(cls, data) -> "Calibration":
        """Разобрать сохранённую поправку; испорченная - ошибка, не догадка."""
        if not isinstance(data, dict) or data.get("version") not in (1, 2):
            raise CalibrationError("Неизвестный формат калибровки.")
        try:
            matrix = tuple(tuple(float(x) for x in row) for row in data["matrix"])
            if data["version"] == 1:  # одна степень на канал - до 2026.10.14
                curves = tuple(_gamma_knots(float(g)) for g in data["gamma"])
            else:
                curves = tuple(
                    tuple((float(d), float(e)) for d, e in k) for k in data["curves"]
                )
            white = data.get("white") or {}
            kelvin = float(white.get("kelvin", DEFAULT_KELVIN))
            tint = float(white.get("tint", DEFAULT_TINT))
        except (KeyError, TypeError, ValueError):
            raise CalibrationError("Калибровка повреждена.") from None
        ok = (
            len(matrix) == 3 and all(len(r) == 3 for r in matrix)
            and all(math.isfinite(x) for r in matrix for x in r)
            and len(curves) == 3
            and all(0 < len(k) <= _MAX_KNOTS for k in curves)
            and all(
                0 < d < 1 and 0 < e < 1 and math.isfinite(d) and math.isfinite(e)
                for k in curves for d, e in k
            )
            and all(
                a[0] < b[0] and a[1] < b[1] for k in curves for a, b in zip(k, k[1:])
            )
            and WHITE_RANGE["kelvin"]["min"] <= kelvin <= WHITE_RANGE["kelvin"]["max"]
            and WHITE_RANGE["tint"]["min"] <= tint <= WHITE_RANGE["tint"]["max"]
        )
        if not ok:
            raise CalibrationError("Калибровка повреждена.")
        return cls(matrix, curves, bool(data.get("white_via_colour", False)), kelvin, tint)

    @classmethod
    def load(cls, data) -> "Calibration | None":
        """Поправка из настроек сущности или None, если её нет или она негодна."""
        if not data:
            return None
        try:
            return cls.from_dict(data)
        except CalibrationError:
            return None


def _solve(a, b) -> list[float]:
    """Решить a·x = b методом Гаусса с выбором главного элемента."""
    n = len(b)
    m = [list(row) + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-300:
            raise CalibrationError(
                "Каналы ленты в кадре неразличимы - камера видит не ленту, а посторонний свет."
            )
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col and m[r][col]:
                f = m[r][col] / m[col][col]
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def _fit_matrix(unit: dict, curves, start) -> list[list[float]]:
    """Вклад каналов по всем кадрам сразу: M · e(d) параллельно кадру.

    С объекта (iPhone, GLEDOPTO SPI): по одним чистым красному, зелёному и
    синему расчёт отказывал почти каждый раз - их телефон искажает и
    обрезает. По всем двадцати кадрам ошибка одного кадра тонет в остальных.

    Яркость кадра неизвестна (автоэкспозиция), поэтому условие - векторное
    произведение кадра и M · e(d) равно нулю; оно линейно по M, и M - вектор
    наименьшего собственного числа суммы этих уравнений (обратная итерация).
    """
    ata = [[0.0] * 9 for _ in range(9)]
    for name, hs, _label in SAMPLES:
        m = unit[name]
        p = [_level(k, d) for k, d in zip(curves, hs_to_rgb(hs))]
        rows = ([0.0] * 9, [0.0] * 9, [0.0] * 9)
        for j in range(3):
            rows[0][6 + j] += m[1] * p[j]
            rows[0][3 + j] -= m[2] * p[j]
            rows[1][j] += m[2] * p[j]
            rows[1][6 + j] -= m[0] * p[j]
            rows[2][3 + j] += m[0] * p[j]
            rows[2][j] -= m[1] * p[j]
        for r in rows:
            for a in range(9):
                if r[a]:
                    for b in range(9):
                        ata[a][b] += r[a] * r[b]
    shift = 1e-9 * (sum(ata[i][i] for i in range(9)) or 1.0)
    shifted = [[ata[i][j] + (shift if i == j else 0.0) for j in range(9)] for i in range(9)]
    x = [v for row in start for v in row]
    for _ in range(40):
        norm = math.sqrt(sum(v * v for v in x)) or 1.0
        x = _solve(shifted, [v / norm for v in x])
    norm = math.sqrt(sum(v * v for v in x)) or 1.0
    x = [v / norm for v in x]
    if x[0] + x[4] + x[8] < 0:
        x = [-v for v in x]
    return [x[0:3], x[3:6], x[6:9]]


def _curves_from(matrix, unit: dict) -> tuple:
    """Точки кривых каналов по всем замерам, где один канал на полную.

    В кадре с командой d свет = M · e(d); M известна, значит M⁻¹ · кадр даёт
    e по каналам с точностью до общего множителя - а он снимается каналом,
    который на полную (e = 1). Точка, которую шум или обрезка камеры увели за
    разумные пределы, отбрасывается, а не роняет всю калибровку.
    """
    inverse = _inv(matrix)
    knots: list[list[tuple[float, float]]] = [[], [], []]
    for name, hs, _label in SAMPLES:
        drive = hs_to_rgb(hs)
        full = max(range(3), key=lambda c: drive[c])
        if drive[full] < 0.999:
            continue
        power = _mul(inverse, unit[name])
        if power[full] <= 0:
            continue
        for ch in range(3):
            if 0.05 < drive[ch] < 0.999:
                level = power[ch] / power[full]
                if 0.01 < level < 1.2:
                    knots[ch].append((drive[ch], min(level, _LEVEL_MAX)))
    curves = []
    for ch in range(3):
        if not knots[ch]:
            curves.append(_gamma_knots(2.2))  # точек нет - обычная кривая
            continue
        curve = []
        for d, e in _monotone(knots[ch]):
            # Две точки с одной долей после сглаживания склеиваются в одну.
            if curve and (d <= curve[-1][0] or e <= curve[-1][1]):
                continue
            curve.append((d, e))
        curves.append(tuple(curve))
    return tuple(curves)


def _first_matrix(unit: dict):
    """Начальное приближение: чистые цвета, сложенные в белый; не вышло - sRGB."""
    try:
        weights = _mul(_inv(_columns(unit["red"], unit["green"], unit["blue"])), unit["white"])
    except CalibrationError:
        weights = [-1.0]
    if min(weights) <= 0:
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    return _columns(*(
        [weights[c] * unit[name][i] for i in range(3)]
        for c, name in enumerate(("red", "green", "blue"))
    ))


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
            "другой свет, задёрните шторы и наведите камеру на белую бумагу."
        )

    # Вклад каналов и их кривые зависят друг от друга - уточняются по очереди,
    # начиная с чистых цветов: на чистом замере они дают ответ сразу.
    matrix = _first_matrix(unit)
    for _ in range(8):
        curves = _curves_from(matrix, unit)
        matrix = _fit_matrix(unit, curves, matrix)
    curves = _curves_from(matrix, unit)

    white_mode = same_as_rgb = None
    if WHITE_MODE[0] in samples:
        raw = samples[WHITE_MODE[0]]
        if _is_dark(raw):
            # Белый режим не светит вовсе: у контроллера белый канал, а на
            # ленте белых светодиодов нет - белый только из RGB.
            same_as_rgb = True
        else:
            seen = _clean(raw, WHITE_MODE[2])
            white_mode = _delta(seen, kelvin_target(DEFAULT_KELVIN, DEFAULT_TINT))
            same_as_rgb = _delta(seen, unit["white"]) < SAME_AS_RGB_TOLERANCE
    calibration = Calibration(
        tuple(tuple(r) for r in matrix),
        curves,
        # Белый режим - тот же свет, что три цвета вместе (белых светодиодов
        # нет), или просто не дневной: белый и температуры - через поправку.
        white_via_colour=bool(same_as_rgb) or (white_mode or 0) > WHITE_MODE_TOLERANCE,
    )

    # Модель обязана объяснять сам замер: иначе он испорчен (свет мигал,
    # телефон сдвинули) и поправка по нему навредит. Смотрим на типичный
    # кадр, а не на худший: насыщенные цвета телефон искажает всегда, и по
    # худшему кадру калибровка отказывала бы на любом телефоне.
    def _miss(hs, seen) -> float:
        try:
            return delta_uv(calibration.predict(hs), seen)
        except ColourError:  # модель не даёт света вовсе - худшее несовпадение
            return 1.0

    deltas = sorted(_miss(hs, unit[name]) for name, hs, _l in SAMPLES)
    misfit = deltas[len(deltas) // 2]
    if misfit > MAX_MISFIT:
        raise CalibrationError(
            "Замер неустойчив: цвета в кадре менялись. Держите телефон "
            "неподвижно и повторите."
        )
    # «До калибровки» - тот белый, что человек видел: белый режим лампы,
    # если он у неё есть, иначе белый из трёх цветов.
    before = white_mode if white_mode is not None else _delta(
        unit["white"], kelvin_target(DEFAULT_KELVIN, DEFAULT_TINT)
    )
    return calibration, {"misfit": round(misfit, 4), "white_before": round(before, 4)}


def refine_white(calibration: Calibration, measured) -> Calibration:
    """Уточнить вклад каналов по замеру исправленного белого.

    Камера видит каналы ленты на краю своей палитры не вполне точно, и белый
    по первой модели может уйти на волосок. Белый - главное, что видно, поэтому
    он замыкается по факту: что модель ждала от команды и что пришло.
    """
    seen = _clean(measured, "белый")
    command = calibration.to_device((0, 0))
    expected = [_level(k, d) for k, d in zip(calibration.curves, hs_to_rgb(command))]
    actual = _mul(calibration._inverse(), seen)
    if min(actual) <= 0 or min(expected) <= 0:
        raise CalibrationError("Замер белого не удался - повторите.")
    gains = [a / e for a, e in zip(actual, expected)]
    mean = sum(gains) / 3
    gains = [g / mean for g in gains]
    matrix = tuple(
        tuple(calibration.matrix[i][j] * gains[j] for j in range(3)) for i in range(3)
    )
    return Calibration(
        matrix, calibration.curves, calibration.white_via_colour,
        calibration.white_kelvin, calibration.white_tint,
    )


def evaluate(measured: dict, calibration: Calibration | None = None) -> dict:
    """Насколько исправленные цвета совпали с целью (с подстройкой белого)."""
    results = []
    for key, hs, kelvin, label in CHECKS:
        if key not in measured:
            continue
        seen = _clean(measured[key], label)
        if calibration is not None:
            target = calibration.target(hs, kelvin)
        else:
            target = kelvin_target(kelvin) if kelvin is not None else hs_target(hs)
        delta = _delta(seen, target)
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

"""Ручная калибровка цвета: цвета, подогнанные человеком на глаз.

Владелец: калибровка камерой «не то», «сделай, чтоб я сам мог добавлять
цвета». Каждая точка - пара: каким цвет должен быть (с колеса выбора) и какую
команду для этого надо послать ленте (подобрана на глаз). Для остальных
цветов команда берётся плавно по соседним точкам.

Цвет - точка на круге оттенков (насыщенность - расстояние от центра, белый -
центр). Каждая точка задаёт сдвиг от цели к команде; для любого цвета сдвиг
берётся средним по точкам с весом обратно квадрату расстояния - ближние точки
решают, дальние почти не мешают, в самих точках сдвиг ровно их. Одна точка
белого сдвигает все цвета одинаково: если зелёный канал ленты слишком силён,
это так для всех цветов.

Модуль без зависимостей от Home Assistant - его проверяют тесты.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .colour_math import kelvin_target, linear_to_srgb, rgb_to_hs, swatch, hs_target

# Цвета Home Assistant (9) + шкала температур (10) + обычные цвета (10).
MAX_POINTS = 40
MAX_LABEL = 40

# Кружки в карточке лампы Home Assistant (frontend src/data/light.ts,
# computeDefaultFavoriteColors): четыре температуры поровну от наименьшей до
# наибольшей у лампы и четыре цвета. Владелец: «именно эти цвета добавь».
HA_TEMPERATURE_COUNT = 4
HA_COLOURS: tuple[tuple[str, str, tuple[int, int, int]], ...] = (
    ("ha_blue", "Голубой", (127, 172, 255)),
    ("ha_purple", "Сиреневый", (215, 150, 255)),
    ("ha_pink", "Розовый", (255, 158, 243)),
    ("ha_red", "Красный", (255, 110, 84)),
)
# Шкала белого: 2700-6500 K через 500 и тёплый 3000.
KELVIN_SCALE = (2700, 3000, 3500, 4000, 4500, 5000, 5500, 6000, 6500)

# Готовые цвета для добавления: ключ, подпись, цвет с колеса или температура.
PRESETS: tuple[tuple[str, str, tuple[float, float] | None, int | None], ...] = (
    ("white", "Белый", (0, 0), None),
    ("red", "Красный", (0, 100), None),
    ("orange", "Оранжевый", (30, 100), None),
    ("yellow", "Жёлтый", (55, 100), None),
    ("green", "Зелёный", (120, 100), None),
    ("sky", "Голубой", (190, 80), None),
    ("blue", "Синий", (240, 100), None),
    ("violet", "Фиолетовый", (275, 80), None),
    ("pink", "Розовый", (330, 60), None),
    ("sand", "Песочный", (38, 40), None),
)


class PointsError(ValueError):
    """Точки повреждены. Текст - для человека, по-русски."""


def kelvin_hs(kelvin: float) -> tuple[float, float]:
    """Белый заданной температуры - как цвет на колесе выбора."""
    return rgb_to_hs([linear_to_srgb(x) for x in kelvin_target(kelvin)])


def ha_temperatures(min_kelvin: int, max_kelvin: int) -> list[int]:
    """Температуры кружков Home Assistant - как их считает сам Home Assistant."""
    step = (max_kelvin - min_kelvin) / (HA_TEMPERATURE_COUNT - 1)
    # Math.round в JavaScript: половина - вверх.
    return [int(math.floor(min_kelvin + step * i + 0.5)) for i in range(HA_TEMPERATURE_COUNT)]


def _preset(key, label, group, hs=None, kelvin=None) -> dict:
    target = kelvin_hs(kelvin) if kelvin is not None else hs
    rgb = kelvin_target(kelvin) if kelvin is not None else hs_target(hs)
    return {
        "key": key,
        "label": label,
        "group": group,
        "target": [round(target[0], 2), round(target[1], 2)],
        "kelvin": kelvin,
        "swatch": swatch(rgb),
    }


def presets(min_kelvin: int = 2700, max_kelvin: int = 6500) -> list[dict]:
    """Готовые цвета для экрана по разделам: цель на колесе и кружок.

    Температуры - в пределах лампы: другие она и в Home Assistant не покажет.
    """
    ha = "Цвета Home Assistant"
    out = [
        _preset(f"ha_{k}", f"{k} K", ha, kelvin=k)
        for k in ha_temperatures(min_kelvin, max_kelvin)
    ]
    for key, label, rgb in HA_COLOURS:
        # Кружок цвета Home Assistant шлёт RGB, а лампа с оттенком получает
        # его как оттенок и насыщенность - так же их считаем и мы.
        out.append(_preset(key, label, ha, hs=rgb_to_hs([c / 255 for c in rgb])))
    scale = "Температура белого"
    out += [
        _preset(f"kelvin_{k}", f"{k} K", scale, kelvin=k)
        for k in KELVIN_SCALE
        if min_kelvin <= k <= max_kelvin
    ]
    out += [
        _preset(key, label, "Цвета", hs=hs, kelvin=kelvin)
        for key, label, hs, kelvin in PRESETS
    ]
    return out


def _xy(hs) -> tuple[float, float]:
    s = min(max(float(hs[1]), 0.0), 100.0) / 100
    a = math.radians(float(hs[0]) % 360)
    return (s * math.cos(a), s * math.sin(a))


def _hs(x: float, y: float) -> tuple[float, float]:
    s = math.hypot(x, y)
    if s < 1e-9:
        return (0.0, 0.0)
    if s > 1:
        x, y, s = x / s, y / s, 1.0
    return (round(math.degrees(math.atan2(y, x)) % 360, 2), round(s * 100, 2))


@dataclass(frozen=True)
class ColourPoints:
    """Точки «цель -> команда», подобранные на глаз."""

    points: tuple[tuple[tuple[float, float], tuple[float, float], str], ...]

    def _shift(self, x: float, y: float) -> tuple[float, float]:
        total = sx = sy = 0.0
        for target, device, _label in self.points:
            tx, ty = _xy(target)
            dx, dy = _xy(device)
            dist2 = (x - tx) ** 2 + (y - ty) ** 2
            if dist2 < 1e-12:
                return (dx - tx, dy - ty)  # ровно в точке - ровно её сдвиг
            w = 1.0 / dist2
            total += w
            sx += w * (dx - tx)
            sy += w * (dy - ty)
        return (sx / total, sy / total)

    # Команда, которую надо послать, чтобы лента показала цвет с колеса.
    def to_device(self, hs) -> tuple[float, float]:
        x, y = _xy(hs)
        sx, sy = self._shift(x, y)
        return _hs(x + sx, y + sy)

    # Что покажет лента на команду - каким цветом колеса её считать.
    def from_device(self, hs) -> tuple[float, float]:
        """Обратное: цель, для которой эта команда и посылается.

        Сдвиг плавный и небольшой, поэтому цель находится простым
        приближением: цель = команда минус сдвиг в цели.
        """
        cx, cy = _xy(hs)
        x, y = cx, cy
        for _ in range(60):
            sx, sy = self._shift(x, y)
            nx, ny = cx - sx, cy - sy
            # Шаг наполовину: соседние точки с разным сдвигом не раскачивают.
            nx, ny = (x + nx) / 2, (y + ny) / 2
            if abs(nx - x) < 1e-7 and abs(ny - y) < 1e-7:
                x, y = nx, ny
                break
            x, y = nx, ny
        result = _hs(x, y)
        return (0.0, 0.0) if result[1] < 0.5 else result

    def as_list(self) -> list[dict]:
        return [
            {
                "target": [round(t[0], 2), round(t[1], 2)],
                "device": [round(d[0], 2), round(d[1], 2)],
                "label": label,
            }
            for t, d, label in self.points
        ]

    @classmethod
    def from_list(cls, data) -> "ColourPoints | None":
        """Разобрать точки; пустой список - ручной калибровки нет."""
        if data in (None, []):
            return None
        if not isinstance(data, list) or len(data) > MAX_POINTS:
            raise PointsError(f"Цветов можно не больше {MAX_POINTS}.")
        points = []
        for item in data:
            try:
                target = (float(item["target"][0]), float(item["target"][1]))
                device = (float(item["device"][0]), float(item["device"][1]))
                label = str(item.get("label") or "")[:MAX_LABEL]
            except (KeyError, TypeError, ValueError, IndexError):
                raise PointsError("Цвет ручной калибровки повреждён.") from None
            values = target + device
            if not all(math.isfinite(v) for v in values) or not all(
                0 <= v[0] <= 360 and 0 <= v[1] <= 100 for v in (target, device)
            ):
                raise PointsError("Цвет ручной калибровки повреждён.")
            points.append((target, device, label))
        # Две точки с одной целью: верна последняя - иначе вес раздваивается.
        unique: dict[tuple[float, float], int] = {}
        for i, (t, _d, _l) in enumerate(points):
            unique[_xy(t)] = i
        kept = sorted(unique.values())
        return cls(tuple(points[i] for i in kept))

    @classmethod
    def load(cls, data) -> "ColourPoints | None":
        """Точки из настроек сущности или None, если их нет или они негодны."""
        try:
            return cls.from_list(data)
        except PointsError:
            return None

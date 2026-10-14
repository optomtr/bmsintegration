"""Цветовые преобразования для калибровки ленты: sRGB, u'v', температура белого.

Без зависимостей от Home Assistant - их проверяют тесты.
"""

from __future__ import annotations

import colorsys
import math

# sRGB (D65) <-> XYZ.
_SRGB_TO_XYZ = (
    (0.4124, 0.3576, 0.1805),
    (0.2126, 0.7152, 0.0722),
    (0.0193, 0.1192, 0.9505),
)
_XYZ_TO_SRGB = (
    (3.2406, -1.5372, -0.4986),
    (-0.9689, 1.8758, 0.0415),
    (0.0557, -0.2040, 1.0570),
)

# Дневной белый D65 - точка, относительно которой камера с балансом 6500 K
# видит «белое»: в линейном sRGB это (1, 1, 1). В координатах «температура +
# сдвиг от линии чёрного тела» D65 лежит на 6504 K чуть выше линии - отсюда
# сдвиг по умолчанию.
DEFAULT_KELVIN = 6500
DEFAULT_TINT = 0.0032


class ColourError(ValueError):
    """Свет, которого нет: ноль во всех каналах."""


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


def hs_target(hs) -> list[float]:
    """Цвет с колеса выбора - в линейном sRGB."""
    return [srgb_to_linear(c) for c in hs_to_rgb(hs)]


def normalised(rgb) -> list[float]:
    """Тот же цвет с наибольшим каналом 1; отрицательное - за краем, в ноль."""
    clipped = [max(0.0, x) for x in rgb]
    top = max(clipped)
    if top <= 0:
        raise ColourError("нет света")
    return [x / top for x in clipped]


def uv_prime(rgb_linear) -> tuple[float, float]:
    """Линейный sRGB -> u'v' (CIE 1976); масштаб значения не имеет."""
    x, y, z = (sum(row[i] * rgb_linear[i] for i in range(3)) for row in _SRGB_TO_XYZ)
    d = x + 15 * y + 3 * z
    if d <= 0:
        raise ColourError("нет света")
    return (4 * x / d, 9 * y / d)


def delta_uv(a_linear, b_linear) -> float:
    ua, va = uv_prime(a_linear)
    ub, vb = uv_prime(b_linear)
    return math.hypot(ua - ub, va - vb)


def swatch(rgb_linear) -> str:
    """Цвет для кружка на экране: #rrggbb."""
    enc = [min(1.0, linear_to_srgb(x)) for x in normalised(rgb_linear)]
    return "#" + "".join(f"{round(c * 255):02x}" for c in enc)


# --------------------------------------------------------------------------- #
# Температура белого
# --------------------------------------------------------------------------- #
def planck_xy(kelvin: float) -> tuple[float, float]:
    """Цветность чёрного тела (приближение Kim et al., 1667-25000 K)."""
    t = min(max(float(kelvin), 1667.0), 25000.0)
    if t <= 4000:
        x = -0.2661239e9 / t**3 - 0.2343589e6 / t**2 + 0.8776956e3 / t + 0.179910
    else:
        x = -3.0258469e9 / t**3 + 2.1070379e6 / t**2 + 0.2226347e3 / t + 0.240390
    if t <= 2222:
        y = -1.1063814 * x**3 - 1.34811020 * x**2 + 2.18555832 * x - 0.20219683
    elif t <= 4000:
        y = -0.9549476 * x**3 - 1.37418593 * x**2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x**3 - 5.87338670 * x**2 + 3.75112997 * x - 0.37001483
    return x, y


def _xy_to_uv60(x, y):
    d = -2 * x + 12 * y + 3
    return 4 * x / d, 6 * y / d


def _uv60_to_xy(u, v):
    d = 2 * u - 8 * v + 4
    return 3 * u / d, 2 * v / d


def white_xy(kelvin: float, tint: float = 0.0) -> tuple[float, float]:
    """Белый заданной температуры, сдвинутый от линии чёрного тела.

    tint - сдвиг Duv поперёк линии: больше нуля - зеленее, меньше - розовее.
    """
    u, v = _xy_to_uv60(*planck_xy(kelvin))
    u2, v2 = _xy_to_uv60(*planck_xy(kelvin + 10))
    du, dv = u2 - u, v2 - v
    length = math.hypot(du, dv) or 1.0
    nu, nv = -dv / length, du / length
    if nv < 0:  # «выше линии» - в сторону зелёного
        nu, nv = -nu, -nv
    return _uv60_to_xy(u + tint * nu, v + tint * nv)


def xy_to_linear(x: float, y: float) -> list[float]:
    """Цветность -> линейный sRGB с наибольшим каналом 1."""
    big_x, big_z = x / y, (1 - x - y) / y
    xyz = (big_x, 1.0, big_z)
    return normalised([sum(row[i] * xyz[i] for i in range(3)) for row in _XYZ_TO_SRGB])


def kelvin_target(kelvin: float, tint: float = 0.0) -> list[float]:
    """Белый заданной температуры в линейном sRGB."""
    return xy_to_linear(*white_xy(kelvin, tint))

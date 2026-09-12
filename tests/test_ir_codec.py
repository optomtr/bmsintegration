"""Перевод длительностей в код Tuya и обратно.

Home Assistant в платформе `infrared` отдаёт команду сырыми длительностями в
микросекундах. Передатчик Tuya такого не принимает - ему нужен свой код:
массив 16-битных чисел, сжатый по FastLZ и завёрнутый в base64. Без этого
перевода излучатель бесполезен.

Проверять на железе нечем, поэтому проверяем то, что проверяемо: обратный
перевод обязан давать ровно исходные длительности.
"""

import base64
import os
import random
import sys
import unittest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "custom_components",
        "bms_integration",
        "core",
    ),
)

from ir_codec import (  # noqa: E402
    MAX_DURATION_US,
    bytes_to_pulses,
    fastlz_compress,
    fastlz_decompress,
    pulses_to_bytes,
    pulses_to_tuya,
    tuya_to_pulses,
)


def nec_frame() -> list[int]:
    """Обычная посылка NEC: заголовок, 32 бита, замыкающий импульс."""
    pulses = [9000, 4500]
    for index in range(32):
        pulses += [560, 1690 if index % 3 == 0 else 560]
    pulses.append(560)
    return pulses


class TheRoundTripIsExact(unittest.TestCase):
    def test_a_real_frame_survives_compression(self):
        frame = nec_frame()
        self.assertEqual(tuya_to_pulses(pulses_to_tuya(frame)), frame)

    def test_an_uncompressed_code_is_read_too(self):
        """Устройства присылают и такие."""
        frame = nec_frame()
        self.assertEqual(
            tuya_to_pulses(pulses_to_tuya(frame, compress=False)), frame
        )

    def test_compression_actually_shrinks_a_repetitive_frame(self):
        """Иначе смысла в нём нет, а код длиннее датапоинт может не принять."""
        frame = nec_frame()
        packed = len(base64.b64decode(pulses_to_tuya(frame)))
        plain = len(base64.b64decode(pulses_to_tuya(frame, compress=False)))
        self.assertLess(packed, plain)

    def test_random_frames_survive(self):
        """Случайные длительности ловят краевые случаи упаковщика."""
        rnd = random.Random(20260912)
        for _ in range(40):
            frame = [rnd.randint(1, 20000) for _ in range(rnd.randint(2, 300))]
            with self.subTest(size=len(frame)):
                self.assertEqual(tuya_to_pulses(pulses_to_tuya(frame)), frame)

    def test_a_long_gap_is_clamped_not_wrapped(self):
        """Два байта - потолок. Обрезать честнее, чем переполниться в мусор."""
        code = pulses_to_tuya([MAX_DURATION_US + 5000, 560])
        self.assertEqual(tuya_to_pulses(code), [MAX_DURATION_US, 560])


class ThePackingIsWhatTheDeviceExpects(unittest.TestCase):
    def test_durations_are_two_bytes_little_endian(self):
        """Порядок байтов перепутать легко, а на железе это молчаливый мусор."""
        self.assertEqual(pulses_to_bytes([0x1234]), b"\x34\x12")
        self.assertEqual(bytes_to_pulses(b"\x34\x12"), [0x1234])

    def test_an_odd_stream_is_refused(self):
        with self.assertRaises(ValueError):
            bytes_to_pulses(b"\x01\x02\x03")


class TheCompressorIsSelfConsistent(unittest.TestCase):
    def test_literals_longer_than_one_chunk(self):
        """Управляющий байт вмещает 32 литерала - граница легко теряется."""
        data = bytes(range(256)) * 2
        self.assertEqual(fastlz_decompress(fastlz_compress(data)), data)

    def test_a_long_repeat_uses_a_back_reference(self):
        data = b"\xaa\xbb" * 200
        packed = fastlz_compress(data)
        self.assertEqual(fastlz_decompress(packed), data)
        self.assertLess(len(packed), len(data) // 4)

    def test_broken_input_is_refused_not_guessed(self):
        with self.assertRaises(ValueError):
            fastlz_decompress(bytes([0xE0, 0xFF, 0xFF]))


if __name__ == "__main__":
    unittest.main(verbosity=2)

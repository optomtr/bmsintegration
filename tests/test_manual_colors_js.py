"""Run the manual_colors.js suite (tests/test_manual_colors_js.mjs) under node.

run_all.py discovers test_*.py only; this wrapper puts the JavaScript suite in
the same run. Without node the suite is skipped, loudly, rather than passing.
"""

import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MANUAL_COLORS_JS = os.path.join(
    HERE, "..", "custom_components", "bms_integration", "panel", "manual_colors.js"
)


class ManualColorsJs(unittest.TestCase):
    def setUp(self):
        self.node = shutil.which("node")
        if not self.node:
            self.skipTest("node не установлен - тесты manual_colors.js пропущены")

    def test_parses_as_a_module(self):
        # `node --check manual_colors.js` молча пропускает синтаксическую
        # ошибку в ES-модуле с расширением .js - проверяем как модуль, через stdin.
        with open(MANUAL_COLORS_JS, "rb") as handle:
            result = subprocess.run(
                [self.node, "--input-type=module", "--check"],
                stdin=handle, capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_node_suite(self):
        result = subprocess.run(
            [self.node, os.path.join(HERE, "test_manual_colors_js.mjs")],
            capture_output=True, text=True, timeout=120,
        )
        print(result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

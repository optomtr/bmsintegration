"""Guards for the panel's privileged surface: panel.py, websocket.py, panel.js.

These three files are the only part of the integration a browser can reach,
and two of their commands write to hardware. Home Assistant itself cannot be
imported here (see ha_stubs.py for why), so the checks are structural: they
parse the sources and assert the properties that must hold, rather than
driving a live connection. A structural check that fails loudly when a
decorator is dropped is worth more than no check at all.

Covered:
1. Every registered WebSocket command requires admin.
2. Nothing that reaches the frontend can carry a secret.
3. The panel is registered admin-only, once per Home Assistant run.
4. The static path is never re-registered (aiohttp cannot unregister one).
5. panel.js escapes every value it interpolates into HTML.
"""

import ast
import os
import re
import shutil
import subprocess
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPONENT = os.path.join(REPO_ROOT, "custom_components", "bms_integration")


def source(*parts: str) -> str:
    with open(os.path.join(COMPONENT, *parts), encoding="utf-8") as handle:
        return handle.read()


def tree(*parts: str) -> ast.Module:
    return ast.parse(source(*parts))


def decorators(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute):
            names.add(target.attr)
        elif isinstance(target, ast.Name):
            names.add(target.id)
    return names


class WebSocketCommandsRequireAdmin(unittest.TestCase):
    """Every mutating command is one missing decorator away from public."""

    def setUp(self):
        self.module = tree("websocket.py")
        self.functions = {
            node.name: node
            for node in ast.walk(self.module)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def registered(self) -> list[str]:
        """Names passed to async_register_command in the registration func."""
        for node in ast.walk(self.module):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != "async_register_websocket_api":
                continue
            # Commands may be passed directly or iterated over from a tuple;
            # either way they appear as ws_* names inside this function.
            registers = any(
                getattr(call.func, "attr", None) == "async_register_command"
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
            )
            self.assertTrue(registers, "nothing calls async_register_command")
            return sorted(
                {
                    name.id
                    for name in ast.walk(node)
                    if isinstance(name, ast.Name) and name.id.startswith("ws_")
                }
            )
        self.fail("async_register_websocket_api not found")

    def test_commands_are_registered(self):
        names = self.registered()
        self.assertTrue(names, "no websocket commands are registered at all")
        # A new command that is never registered is dead code; one that is
        # registered but missing below is caught by the next test.
        for name in names:
            self.assertIn(name, self.functions, f"{name} is registered but undefined")

    def test_every_command_requires_admin(self):
        for name in self.registered():
            with self.subTest(command=name):
                self.assertIn(
                    "require_admin",
                    decorators(self.functions[name]),
                    f"{name} is reachable by any logged-in user",
                )

    def test_every_command_declares_a_schema(self):
        for name in self.registered():
            with self.subTest(command=name):
                self.assertIn(
                    "websocket_command",
                    decorators(self.functions[name]),
                    f"{name} accepts unvalidated input",
                )

    def test_mutating_commands_are_audited(self):
        """A raw datapoint write must name the operator in the log."""
        body = ast.get_source_segment(source("websocket.py"), self.functions["ws_set_dp"])
        self.assertIn("_LOGGER.warning", body)
        self.assertIn("connection.user", body)


class SecretsNeverReachTheFrontend(unittest.TestCase):
    def test_secret_keys_cover_the_device_key(self):
        websocket = source("websocket.py")
        for key in ("local_key", "client_secret", "client_id", "user_id", "token"):
            self.assertIn(f'"{key}"', websocket, f"{key} is not in SECRET_KEYS")

    def test_device_config_is_redacted_before_sending(self):
        websocket = source("websocket.py")
        # The device card and the diagnostics dump both render this config.
        self.assertIn("_redacted(", websocket)
        self.assertNotIn('"config": config,', websocket)

    def test_panel_export_strips_identifiers(self):
        panel_js = source("panel", "panel.js")
        export = panel_js[panel_js.index('if (a === "export")') :][:900]
        for field in ("host", "device_id", "node_id", "gateway_id"):
            self.assertIn(field, export, f"the export does not strip {field}")


class PanelRegistration(unittest.TestCase):
    def test_panel_is_admin_only(self):
        self.assertIn("require_admin=True", source("panel.py"))

    def test_registration_flag_is_claimed_before_the_first_await(self):
        """Two entries set up concurrently each added a ring-log handler."""
        panel = source("panel.py")
        setup = panel[panel.index("async def async_setup_panel") :]
        setup = setup[: setup.index("@callback")]
        claim = setup.index("domain_data[DATA_PANEL_REGISTERED] = True")
        first_await = setup.index("await ")
        self.assertLess(
            claim, first_await, "the guard is set after an await: two entries race"
        )

    def test_static_path_is_registered_once_per_run(self):
        """aiohttp cannot unregister a static route; re-registering leaks."""
        panel = source("panel.py")
        self.assertIn("DATA_STATIC_REGISTERED", panel)
        remove = panel[panel.index("def async_remove_panel") :]
        self.assertNotIn(
            "DATA_STATIC_REGISTERED", remove, "the static flag is cleared on removal"
        )

    def test_log_buffer_survives_a_reload(self):
        panel = source("panel.py")
        self.assertIn("_async_setup_log_buffer", panel)

    def test_log_buffer_is_bounded(self):
        panel = source("panel.py")
        self.assertIn("maxlen=LOG_BUFFER_SIZE", panel)
        size = re.search(r"LOG_BUFFER_SIZE = (\d+)", panel)
        self.assertIsNotNone(size)
        self.assertLessEqual(int(size.group(1)), 2000, "the ring buffer is not a log file")


class PanelFrontend(unittest.TestCase):
    def setUp(self):
        self.js = source("panel", "panel.js")

    def test_kv_escapes_its_values(self):
        """kv() is fed the host of a device heard on an open UDP port."""
        kv = self.js[self.js.index("const kv = (rows)") :][:600]
        self.assertIn("${esc(v)}", kv)
        self.assertNotIn("${v}<", kv, "kv() interpolates a raw value")

    def test_no_double_escaping_at_kv_call_sites(self):
        # esc() around a value that kv() escapes again renders as &amp;lt;.
        self.assertNotIn('["Последняя ошибка", esc(', self.js)
        self.assertNotIn('["Node ID", esc(', self.js)

    def test_late_responses_cannot_overwrite_the_open_device(self):
        refresh = self.js[self.js.index("async refresh(") :][:4200]
        self.assertIn("const devId = this.s.deviceId", refresh)
        self.assertIn("this.s.deviceId === devId", refresh)

    def test_poll_has_an_in_flight_guard_and_backoff(self):
        refresh = self.js[self.js.index("async refresh(") :][:4200]
        self.assertIn("if (this._inflight) return", refresh)
        self.assertIn("_retryAt", refresh)
        self.assertIn('document.visibilityState === "hidden"', self.js)

    def test_silent_repaint_keeps_focus(self):
        paint = self.js[self.js.index("paint(silent = false)") :][:1400]
        self.assertIn("holdsLiveInput()", paint)

    def test_actions_do_not_resolve_targets_from_the_filtered_list(self):
        self.assertIn("deviceRow(id)", self.js)
        self.assertNotIn("this.rows().find((r) => r.device_id === id)", self.js)

    def test_parses_as_javascript(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        result = subprocess.run(
            [node, "--check", os.path.join(COMPONENT, "panel", "panel.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

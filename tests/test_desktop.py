"""Desktop tests: in-memory registry and tray, real loopback HTTP only.

Run with python -m unittest discover -s tests -p test_desktop.py -v.
"""
import ctypes
import http.client
import ntpath
import os
import sys
import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import __main__ as entry
from harness import desktop, server


class FakeRegistry:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.exists = False
        self.values = {}
        self.calls = []
        self.failure = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def _call(self, name):
        self.calls.append(name)
        if self.failure == name:
            raise PermissionError(name)

    def OpenKey(self, hive, path, reserved, access):
        assert (hive, path) == (self.HKEY_CURRENT_USER, desktop._RUN_KEY)
        self._call("open")
        if not self.exists:
            raise FileNotFoundError(path)
        return self

    def CreateKeyEx(self, hive, path, reserved, access):
        assert (hive, path) == (self.HKEY_CURRENT_USER, desktop._RUN_KEY)
        self._call("create")
        self.exists = True
        return self

    def QueryValueEx(self, key, name):
        self._call("query")
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name]

    def SetValueEx(self, key, name, reserved, kind, value):
        self._call("set")
        self.values[name] = value, kind

    def DeleteValue(self, key, name):
        self._call("delete")
        del self.values[name]


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.registry = FakeRegistry()
        self.stack.enter_context(patch.dict(sys.modules, {"winreg": self.registry}))
        self.stack.enter_context(patch.object(sys, "platform", "win32"))
        self.stack.enter_context(patch.object(sys, "frozen", False, create=True))
        self.stack.enter_context(patch.dict(os.environ, {"LH_HEADLESS": "0"}))
        self.stack.enter_context(patch.object(desktop, "_runtime", None))

    def test_source_command_quotes_spaces_and_cyrillic(self):
        with patch.object(desktop.os, "path", ntpath), \
                patch.object(sys, "executable", r"C:\Программы Python\python.exe"), \
                patch.object(desktop, "__file__", r"C:\Мой Харнес\harness\desktop.py"):
            self.assertEqual(desktop._autostart_command(),
                             '"C:\\Программы Python\\python.exe" "C:\\Мой Харнес\\lh_entry.py" '
                             'serve --tray --background')

    def test_frozen_command_quotes_executable_only(self):
        with patch.object(desktop.os, "path", ntpath), \
                patch.object(sys, "executable", r"C:\Мой Харнес\LocalHarness.exe"), \
                patch.object(sys, "frozen", True):
            self.assertEqual(desktop._autostart_command(),
                             '"C:\\Мой Харнес\\LocalHarness.exe" serve --tray --background')

    def test_source_command_uses_absolute_entry_not_argv(self):
        with patch.object(sys, "argv", ["unrelated.py"]):
            command = desktop._autostart_command()
        self.assertIn("lh_entry.py", command)
        self.assertNotIn("unrelated.py", command)
        expected = os.path.abspath(os.path.join(os.path.dirname(desktop.__file__), "..", "lh_entry.py"))
        self.assertTrue(os.path.isfile(expected))
        self.assertIn(expected, command)

    def test_status_and_disable_missing_do_not_create_registry_key(self):
        self.assertEqual(desktop.status(), {"supported": True, "enabled": False, "tray": False})
        self.assertFalse(desktop.set_autostart(False)["enabled"])
        self.assertFalse(self.registry.exists)
        self.assertNotIn("create", self.registry.calls)

    def test_enable_disable_exact_owned_value(self):
        self.registry.values["OtherApplication"] = ("other.exe", self.registry.REG_SZ)
        self.assertTrue(desktop.set_autostart(True)["enabled"])
        self.assertEqual(self.registry.values[desktop._RUN_NAME],
                         (desktop._autostart_command(), self.registry.REG_SZ))
        self.assertTrue(desktop.set_autostart(True)["enabled"])
        self.assertFalse(desktop.set_autostart(False)["enabled"])
        self.assertNotIn(desktop._RUN_NAME, self.registry.values)
        self.assertIn("OtherApplication", self.registry.values)
        self.assertFalse(desktop.set_autostart(False)["enabled"])

    def test_foreign_commands_and_wrong_registry_types_are_not_owned(self):
        self.registry.exists = True
        for value in [(desktop._autostart_command() + " --extra", self.registry.REG_SZ),
                      (desktop._autostart_command(), 2), ("other.exe", self.registry.REG_SZ)]:
            with self.subTest(value=value):
                self.registry.values[desktop._RUN_NAME] = value
                self.assertFalse(desktop.status()["enabled"])
                self.assertFalse(desktop.set_autostart(False)["enabled"])
                self.assertEqual(self.registry.values[desktop._RUN_NAME], value)
                with self.assertRaises(FileExistsError):
                    desktop.set_autostart(True)

    def test_registry_failures_propagate(self):
        self.registry.exists = True
        for operation in ("open", "query", "create", "set", "delete"):
            with self.subTest(operation=operation):
                self.registry.values[desktop._RUN_NAME] = (desktop._autostart_command(), self.registry.REG_SZ)
                self.registry.failure = operation
                with self.assertRaises(PermissionError):
                    if operation in ("open", "query"):
                        desktop.status()
                    else:
                        desktop.set_autostart(operation != "delete")

    def test_unsupported_platform_has_no_registry_access(self):
        with patch.object(sys, "platform", "linux"):
            self.assertEqual(desktop.status(), {"supported": False, "enabled": False, "tray": False})
            with self.assertRaises(OSError):
                desktop.set_autostart(True)
        self.assertEqual(self.registry.calls, [])

    def test_autostart_requires_explicit_boolean(self):
        for enabled in ("false", None, 1, 0):
            with self.subTest(enabled=enabled), self.assertRaises(TypeError):
                desktop.set_autostart(enabled)
        self.assertEqual(self.registry.calls, [])

    def test_tray_flag_precedence(self):
        cases = [
            (False, [], "0", True, False),
            (False, ["--tray"], "0", True, True),
            (True, [], "0", True, True),
            (True, ["--background"], "0", True, True),
            (True, ["--no-tray"], "0", True, False),
            (True, ["--tray", "--no-tray"], "0", True, False),
            (True, ["--tray"], "1", True, False),
            (False, ["--tray"], "0", False, False),
        ]
        for frozen, args, headless, windows, expected in cases:
            with self.subTest(args=args, frozen=frozen, headless=headless, windows=windows), \
                    patch.object(sys, "frozen", frozen), \
                    patch.object(sys, "platform", "win32" if windows else "linux"), \
                    patch.dict(os.environ, {"LH_HEADLESS": headless}):
                self.assertEqual(desktop.should_use_tray(args), expected)

    def _cli(self, args, *, frozen=False, headless="0", existing=False):
        cfg = {"host": "127.0.0.1", "port": 7788}
        with patch.object(sys, "frozen", frozen), patch.dict(os.environ, {"LH_HEADLESS": headless}), \
                patch.object(entry, "force_utf8"), patch.object(entry, "load", return_value=cfg), \
                patch.object(entry, "port_open", return_value=existing), patch("builtins.print"), \
                patch.object(entry, "open_ui") as opener, patch.object(desktop, "run") as tray, \
                patch.object(server, "serve") as serve, patch.object(entry.threading, "Thread") as thread:
            result = entry.main(args)
        self.assertEqual(result, 0)
        return cfg, opener, tray, serve, thread

    def test_frozen_default_and_source_explicit_tray(self):
        for args, frozen in [([], True), (["serve", "--tray"], False), (["--tray"], False)]:
            with self.subTest(args=args, frozen=frozen):
                cfg, opener, tray, serve, thread = self._cli(args, frozen=frozen)
                tray.assert_called_once_with(cfg, open_browser=True)
                serve.assert_not_called()
                thread.assert_not_called()
                opener.assert_not_called()

    def test_background_overrides_open_for_tray_and_existing_server(self):
        for existing in (True, False):
            with self.subTest(existing=existing):
                cfg, opener, tray, serve, thread = self._cli(
                    ["serve", "--tray", "--open", "--background"], existing=existing)
                opener.assert_not_called()
                thread.assert_not_called()
                serve.assert_not_called()
                if existing:
                    tray.assert_not_called()
                else:
                    tray.assert_called_once_with(cfg, open_browser=False)

    def test_headless_and_no_tray_preserve_console_server(self):
        for args, headless, browser in [
                (["serve", "--tray"], "1", False),
                (["serve", "--tray", "--open"], "1", True),
                (["serve", "--tray", "--no-tray"], "0", True),
                (["--no-tray", "--background"], "0", False)]:
            with self.subTest(args=args, headless=headless):
                cfg, opener, tray, serve, thread = self._cli(args, frozen=True, headless=headless)
                serve.assert_called_once_with(cfg)
                tray.assert_not_called()
                self.assertEqual(thread.called, browser)
                opener.assert_not_called()

    def test_normal_source_serve_preserves_delayed_browser(self):
        cfg, opener, tray, serve, thread = self._cli([])
        tray.assert_not_called()
        serve.assert_called_once_with(cfg)
        thread.return_value.start.assert_called_once_with()
        self.assertTrue(thread.call_args.kwargs["daemon"])
        with patch.object(entry.time, "sleep") as sleep, patch.object(entry, "open_ui") as open_ui:
            thread.call_args.kwargs["target"]()
            sleep.assert_called_once_with(2.0)
            open_ui.assert_called_once_with(cfg)

    def test_existing_server_is_never_owned(self):
        for headless, browser in [("0", True), ("1", False)]:
            with self.subTest(headless=headless):
                cfg, opener, tray, serve, thread = self._cli([], frozen=True, existing=True, headless=headless)
                self.assertEqual(opener.called, browser)
                tray.assert_not_called()
                serve.assert_not_called()
                thread.assert_not_called()

    def test_console_hiding_requires_frozen_exclusive_visible_console(self):
        kernel32, user32 = Mock(), Mock()
        kernel32.GetConsoleWindow.return_value = 123
        user32.IsWindowVisible.return_value = True
        def processes(array, size):
            array[0] = os.getpid()
            return 1
        kernel32.GetConsoleProcessList.side_effect = processes
        windll = SimpleNamespace(kernel32=kernel32, user32=user32)
        with patch.object(ctypes, "windll", windll, create=True):
            self.assertIsNone(desktop._hide_owned_console())
            kernel32.GetConsoleWindow.assert_not_called()
            with patch.object(sys, "frozen", True):
                self.assertEqual(desktop._hide_owned_console(), 123)
                user32.ShowWindow.assert_called_once_with(123, 0)
                desktop._restore_console(123)
                user32.ShowWindow.assert_called_with(123, 5)
                user32.ShowWindow.reset_mock()
                for count in (0, 2, 8):
                    kernel32.GetConsoleProcessList.side_effect = lambda array, size, n=count: n
                    self.assertIsNone(desktop._hide_owned_console())
                user32.ShowWindow.assert_not_called()
                kernel32.GetConsoleProcessList.side_effect = processes
                user32.IsWindowVisible.return_value = False
                self.assertIsNone(desktop._hide_owned_console())
                user32.ShowWindow.assert_not_called()

    def test_exclusive_pseudoconsole_is_detached_not_hidden(self):
        kernel32, user32 = Mock(), Mock()
        kernel32.GetConsoleWindow.return_value = 123
        user32.IsWindowVisible.return_value = True
        def processes(array, size):
            array[0] = os.getpid()
            return 1
        kernel32.GetConsoleProcessList.side_effect = processes
        def class_name(hwnd, buffer, size):
            buffer.value = "PseudoConsoleWindow"
            return len(buffer.value)
        user32.GetClassNameW.side_effect = class_name
        windll = SimpleNamespace(kernel32=kernel32, user32=user32)
        with patch.object(ctypes, "windll", windll, create=True), \
                patch.object(sys, "frozen", True):
            self.assertIsNone(desktop._hide_owned_console())
        kernel32.FreeConsole.assert_called_once_with()
        user32.ShowWindow.assert_not_called()
        desktop._restore_console(None)
        user32.ShowWindow.assert_not_called()

    def test_icon_is_drawn_with_pillow(self):
        image = desktop._icon_image()
        self.assertEqual(image.size, (64, 64))
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((15, 20)), (255, 255, 255, 255))

    def _run_tray(self, action, *, open_browser=False, fail_run=False, fail_setup=False):
        test = self
        icons = []
        class Icon:
            def __init__(self, name, image, title, menu):
                self.menu = menu
                self._visible = False
                self.stopped = threading.Event()
                self.setup_thread = None
                icons.append(self)

            @property
            def visible(self):
                return self._visible

            @visible.setter
            def visible(self, value):
                if fail_setup and value:
                    raise OSError("cannot show tray icon")
                self._visible = value

            def run(self, setup):
                if fail_run:
                    raise RuntimeError("tray unavailable")
                self.setup_thread = threading.Thread(target=setup, args=(self,))
                self.setup_thread.start()
                self.setup_thread.join(3)
                test.assertFalse(self.setup_thread.is_alive(), "setup deadlocked")
                try:
                    action(self)
                    test.assertTrue(self.stopped.wait(3), "exit deadlocked")
                finally:
                    # Avoid leaving a live HTTP loop if a test assertion fails.
                    desktop._runtime.request_exit()

            def stop(self):
                if self.setup_thread:
                    test.assertIsNot(threading.current_thread(), self.setup_thread)
                    self.setup_thread.join(3)
                    test.assertFalse(self.setup_thread.is_alive())
                self.stopped.set()

        fake_tray = SimpleNamespace(Icon=Icon, Menu=lambda *items: items,
                                    MenuItem=lambda label, callback, **kw: SimpleNamespace(
                                        label=label, callback=callback, **kw))
        with patch.dict(sys.modules, {"pystray": fake_tray}), \
                patch.object(desktop, "_hide_owned_console", return_value=123) as hide, \
                patch.object(desktop, "_restore_console") as restore:
            desktop.run({"host": "127.0.0.1", "port": 0}, open_browser=open_browser)
        restore.assert_called_once_with(123)
        hide.assert_called_once_with()
        self.assertIsNone(desktop._runtime)
        self.assertFalse(desktop.status()["tray"])
        self.assertNotIn("set", self.registry.calls)
        return icons[0]

    def test_tray_open_exit_and_real_http_shutdown(self):
        order = []
        captured = []
        def stop_bridge():
            # A callback may reacquire RUNS_LOCK or request Exit again.
            with server.RUNS_LOCK:
                order.append("bridge")
            desktop._runtime.request_exit()
        bridge = Mock(request_stop=Mock(side_effect=stop_bridge))
        finished = Mock()
        runs = {"active": SimpleNamespace(finished=False, bridge=bridge),
                "done": SimpleNamespace(finished=True, bridge=finished)}
        def action(icon):
            runtime = desktop._runtime
            captured.append(runtime)
            self.assertTrue(desktop.status()["tray"])
            self.assertTrue(icon.visible)
            connection = http.client.HTTPConnection(*runtime.httpd.server_address, timeout=2)
            try:
                connection.request("GET", "/not-a-route")
                self.assertEqual(connection.getresponse().status, 404)
            finally:
                connection.close()
            icon.menu[0].callback(icon, icon.menu[0])
            original_shutdown = runtime.httpd.shutdown
            def shutdown():
                self.assertEqual(order, ["bridge"])
                self.assertIsNot(threading.current_thread(), runtime._http_thread)
                order.append("shutdown")
                original_shutdown()
            runtime.httpd.shutdown = shutdown
            callers = [threading.Thread(target=icon.menu[1].callback, args=(icon, icon.menu[1]))
                       for _ in range(5)]
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join(3)
                self.assertFalse(caller.is_alive(), "concurrent Exit deadlocked")
        with patch.object(server, "RUNS", runs), patch.object(entry, "open_ui") as opener:
            icon = self._run_tray(action)
            opener.assert_called_once_with({"host": "127.0.0.1", "port": 0})
        self.assertEqual([item.label for item in icon.menu], ["Open", "Exit"])
        self.assertTrue(icon.menu[0].default)
        self.assertEqual(order, ["bridge", "shutdown"])
        self.assertEqual(captured[0].httpd.socket.fileno(), -1)
        self.assertFalse(captured[0]._http_thread.is_alive())
        bridge.request_stop.assert_called_once_with()
        finished.request_stop.assert_not_called()

    def test_bridge_failure_does_not_prevent_remaining_stops(self):
        failing = Mock(request_stop=Mock(side_effect=RuntimeError("stop failed")))
        healthy = Mock()
        runs = {"bad": SimpleNamespace(finished=False, bridge=failing),
                "good": SimpleNamespace(finished=False, bridge=healthy)}
        with patch.object(server, "RUNS", runs), self.assertLogs(level="ERROR"):
            self._run_tray(lambda icon: icon.menu[1].callback(icon, icon.menu[1]))
        failing.request_stop.assert_called_once_with()
        healthy.request_stop.assert_called_once_with()

    def test_exit_cancels_delayed_browser(self):
        def action(icon):
            icon.menu[1].callback(icon, icon.menu[1])
        with patch.object(desktop.threading, "Timer") as timer, patch.object(server, "RUNS", {}):
            self._run_tray(action, open_browser=True)
            timer.assert_called_once()
            self.assertEqual(timer.call_args.args[0], 2.0)
            timer.return_value.start.assert_called_once()
            timer.return_value.cancel.assert_called_once()
            with patch.object(entry, "open_ui") as opener:
                timer.call_args.args[1]()
                opener.assert_not_called()

    def test_new_chat_is_rejected_once_shutdown_starts(self):
        def action(icon):
            runtime = desktop._runtime
            runtime.stopping.set()
            connection = http.client.HTTPConnection(*runtime.httpd.server_address, timeout=2)
            try:
                connection.request("POST", "/api/chat", body='{"text":"do not run"}',
                                   headers={"Content-Type": "application/json"})
                self.assertEqual(connection.getresponse().status, 503)
            finally:
                connection.close()
            icon.menu[1].callback(icon, icon.menu[1])
        with patch.object(server, "RUNS", {}), patch.object(server.Handler, "handle_chat") as chat:
            self._run_tray(action)
        chat.assert_not_called()

    def test_inflight_chat_bridge_is_included_in_exit_snapshot(self):
        entered, release = threading.Event(), threading.Event()
        bridge = Mock()
        responses = []
        def register_chat(handler, body):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test did not release chat")
            with server.RUNS_LOCK:
                server.RUNS["late"] = SimpleNamespace(finished=False, bridge=bridge)
            server._json_response(handler, {"ok": True})
        def action(icon):
            runtime = desktop._runtime
            def chat_request():
                connection = http.client.HTTPConnection(*runtime.httpd.server_address, timeout=3)
                try:
                    connection.request("POST", "/api/chat", body='{"text":"test"}',
                                       headers={"Content-Type": "application/json"})
                    responses.append(connection.getresponse().status)
                finally:
                    connection.close()
            request = threading.Thread(target=chat_request)
            request.start()
            try:
                self.assertTrue(entered.wait(3))
                runtime.request_exit()
            finally:
                release.set()
                request.join(3)
            self.assertFalse(request.is_alive())
        with patch.object(server, "RUNS", {}), patch.object(server.Handler, "handle_chat", register_chat):
            self._run_tray(action)
        self.assertEqual(responses, [200])
        bridge.request_stop.assert_called_once_with()

    def test_tray_setup_thread_failure_stops_without_self_join(self):
        with patch.object(server, "RUNS", {}), self.assertRaisesRegex(RuntimeError, "Could not start") as raised:
            self._run_tray(lambda icon: None, fail_setup=True)
        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertIsNone(desktop._runtime)

    def test_tray_start_failure_closes_server_and_clears_state(self):
        captured = []
        original_server = server.ThreadingHTTPServer
        def create(*args, **kwargs):
            httpd = original_server(*args, **kwargs)
            captured.append(httpd)
            return httpd
        with patch.object(server, "ThreadingHTTPServer", side_effect=create), \
                patch.object(server, "RUNS", {}), self.assertRaisesRegex(RuntimeError, "tray unavailable"):
            self._run_tray(lambda icon: None, fail_run=True)
        self.assertEqual(captured[0].socket.fileno(), -1)
        self.assertIsNone(desktop._runtime)


if __name__ == "__main__":
    unittest.main()

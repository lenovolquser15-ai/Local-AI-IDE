import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["LH_DATA"] = os.path.join(ROOT, "tests", ".tmp")
os.environ["LH_CONFIG"] = os.path.join(ROOT, "tests", ".tmp", "no-such-config.json")

from harness import tools  # noqa: E402
from harness.server import Run, SseBridge  # noqa: E402
from harness.tools import Toolkit  # noqa: E402


class FakeUser32:
    """Records Win32 input calls instead of moving the real mouse/keyboard."""

    def __init__(self):
        self.calls = []

    def MapVirtualKeyW(self, code, _mode):
        return int(code) & 0xFF

    def keybd_event(self, vk, scan, flags, extra):
        self.calls.append(("key", vk, flags))

    def mouse_event(self, flags, dx, dy, data, extra):
        self.calls.append(("mouse", flags, dx, dy))

    def GetAsyncKeyState(self, _code):
        return 0

    def ShowWindow(self, *_args):
        return 1

    def SetForegroundWindow(self, *_args):
        return 1

    def GetWindowRect(self, *_args):
        return 0

    def SetCursorPos(self, x, y):
        self.calls.append(("cursor", x, y))
        return 1


@pytest.fixture
def fake_input(monkeypatch):
    fake = FakeUser32()
    monkeypatch.setattr(tools, "_user32", lambda: fake)
    return fake


def test_game_control_tool_exists(fake_input):
    tk = Toolkit(cfg={})
    res = tk.game_control(action="check", window=None)
    assert res.get("ok") is True
    assert "executed" in res


def test_game_control_without_args_does_not_click(fake_input):
    tk = Toolkit(cfg={})
    res = tk.game_control()
    assert res["executed"][0]["action"] == "check"
    assert not [c for c in fake_input.calls if c[0] == "mouse"]


def test_game_control_invalid_key(fake_input):
    tk = Toolkit(cfg={})
    res = tk.game_control(action="press", key="nonexistent_key_123")
    assert res.get("ok") is True
    assert any(not r.get("ok") for r in res.get("executed", []))


def test_game_control_action_sequence(fake_input):
    tk = Toolkit(cfg={})
    seq = [
        {"action": "wait", "seconds": 0.01},
        {"action": "scroll", "amount": 0},
        {"action": "look", "dx": 0, "dy": 0},
    ]
    res = tk.game_control(actions=seq)
    assert res.get("ok") is True
    assert len(res.get("executed", [])) == 3
    assert all(r.get("ok") for r in res.get("executed", []))


def test_game_control_look_and_window_filter(fake_input):
    tk = Toolkit(cfg={})
    res = tk.game_control(action="look", dx=10, dy=-5)
    assert res.get("ok") is True
    execs = res.get("executed", [])
    assert len(execs) == 1
    assert execs[0]["action"] == "look"
    assert execs[0]["dx"] == 10
    assert execs[0]["dy"] == -5
    assert ("mouse", 0x0001, 10, -5) in fake_input.calls


def test_game_control_missing_window_stops(fake_input, monkeypatch):
    tk = Toolkit(cfg={})
    monkeypatch.setattr(tools, "_win", lambda *_a, **_kw: {"windows": []})
    res = tk.game_control(action="press", key="w", window="no-such-window-12345")
    assert res.get("ok") is False
    assert "не найдено" in res.get("error", "").lower()


def test_sse_run_broadcast_and_indices():
    run = Run("test-sid")
    bridge = SseBridge(run)
    bridge.emit({"type": "user", "text": "hello"})
    bridge.emit({"type": "tool", "tool": "test", "ok": True})
    bridge.emit({"type": "done", "text": "result"})

    assert len(run.events) == 3
    assert run.events[0]["_i"] == 0
    assert run.events[1]["_i"] == 1
    assert run.events[2]["_i"] == 2
    assert run.finished is True


def test_stale_since_reset():
    run = Run("test-stale")
    run.broadcast({"type": "user", "text": "turn2"})
    run.broadcast({"type": "done", "text": "done2"})
    # If client sends since=16 from turn 1, but new run has only 2 events:
    since = 16
    idx = max(0, since)
    if idx > len(run.events):
        idx = 0
    assert idx == 0
    batch = run.events[idx:]
    assert len(batch) == 2

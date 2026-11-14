import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# memory.json, notes.md and generated shots must not land in the app's real data/
os.environ["LH_DATA"] = os.path.join(ROOT, "tests", ".tmp")
os.environ["LH_CONFIG"] = os.path.join(ROOT, "tests", ".tmp", "no-such-config.json")

from harness import memory, comfyui  # noqa: E402
from harness import server as server_mod  # noqa: E402
from harness.server import make_qr_svg, get_lan_ip, is_allowed_client  # noqa: E402
from harness.tools import Toolkit  # noqa: E402
from harness import schemas  # noqa: E402


def test_memory_crud():
    memory.clear_facts()
    assert len(memory.list_facts()) == 0

    res = memory.add_fact("Пользователь любит Python 3.10")
    assert res.get("ok") is True
    fact = res.get("fact")
    assert fact["text"] == "Пользователь любит Python 3.10"

    # duplicate check
    dup = memory.add_fact("Пользователь любит Python 3.10")
    assert dup.get("duplicate") is True

    # search
    found = memory.search_facts("python")
    assert len(found) == 1
    assert found[0]["id"] == fact["id"]

    # system prompt block
    block = memory.system_prompt_block()
    assert "Пользователь любит Python 3.10" in block

    # delete
    del_res = memory.delete_fact(fact["id"])
    assert del_res.get("ok") is True
    assert len(memory.list_facts()) == 0


def test_tools_remember_and_recall():
    memory.clear_facts()
    tk = Toolkit(cfg={"memory_enabled": True})
    res = tk.remember("Проект LocalHarness находится в c:\\LocalHarness")
    assert res.get("ok") is True

    recall_res = tk.recall("LocalHarness")
    assert recall_res.get("ok") is True
    assert recall_res.get("count") >= 1
    memory.clear_facts()


def test_comfyui_disabled():
    res = comfyui.generate_image({"image_gen_enabled": False}, "a beautiful cat")
    assert res.get("ok") is False
    assert "отключена" in res.get("error", "").lower()


def test_qr_code_generation():
    ip = get_lan_ip()
    assert ip
    svg = make_qr_svg(f"http://{ip}:7788")
    assert "<svg" in svg
    assert "</svg>" in svg


def test_system_prompt_with_memory():
    memory.clear_facts()
    memory.add_fact("Важное правило: всегда писать чистый код")
    sp_on = schemas.build_system_prompt({"memory_enabled": True})
    assert "всегда писать чистый код" in sp_on

    sp_off = schemas.build_system_prompt({"memory_enabled": False})
    assert "всегда писать чистый код" not in sp_off
    memory.clear_facts()


def test_lan_ip_prefers_wifi_over_vpn(monkeypatch):
    import socket as _socket
    from types import SimpleNamespace
    import psutil

    def addr(ip):
        return SimpleNamespace(family=_socket.AF_INET, address=ip)

    monkeypatch.setattr(psutil, "net_if_addrs", lambda: {
        "throne-tun": [addr("172.19.0.1")],
        "Radmin VPN": [addr("26.1.2.3")],
        "Wi-Fi": [addr("192.168.10.163")],
    })
    monkeypatch.setattr(psutil, "net_if_stats", lambda: {
        "throne-tun": SimpleNamespace(isup=True),
        "Radmin VPN": SimpleNamespace(isup=True),
        "Wi-Fi": SimpleNamespace(isup=True),
    })
    candidates = server_mod._lan_candidates()
    assert candidates[0] == "192.168.10.163"
    assert "172.19.0.1" not in candidates


def test_lan_access_guard():
    off = {"lan_access": False}
    assert is_allowed_client("127.0.0.1", off) is True
    assert is_allowed_client("::1", off) is True
    assert is_allowed_client("192.168.1.50", off) is False

    on = {"lan_access": True}
    assert is_allowed_client("192.168.1.50", on) is True
    assert is_allowed_client("::ffff:192.168.1.50", on) is True
    assert is_allowed_client("10.0.0.7", on) is True
    assert is_allowed_client("8.8.8.8", on) is False
    assert is_allowed_client("not-an-ip", on) is False

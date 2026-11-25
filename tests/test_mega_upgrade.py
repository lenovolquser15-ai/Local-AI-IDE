"""Comprehensive test suite for the Mega-Upgrade (v1.0.8 Studio 3.0 / 2.0 wave):
1. Reference LoRA project unpacked in lora_train_reference/ + dataset_Ami.jsonl
2. Training Studio 3.0 (Universal PEFT CUDA/ROCm/CPU script, 0-VRAM layer inspector, 15 quant types, speed/ETA metrics)
3. Dashboard 2.0 (60-min buffer, TPS & tool call counters, top processes, CSV export, counter reset)
4. Cluster 2.0 (rag_chunk_score & matrix_ops tasks, toggle_llm_node, native llama.cpp ggml-rpc-server status)
5. Voice 2.0 (Edge Neural TTS MP3 synthesis, pitch/speed, sentence splitter for streaming calls)
6. Plugins 2.0 (hot-reload update_plugin_code, per-plugin SETTINGS, author plugin gguf_toolkit.py GGUF v3 binary inspector)
7. WebUI 100% byte-parity between webui/ and harness/webui/
"""
import json
import os
import struct
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from harness import cluster as cluster_mod
from harness import config as config_mod
from harness import dashboard as dashboard_mod
from harness import plugins, server, tools, unsloth_trainer as ut, voice


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    sess_dir = data_dir / "sessions"
    work_dir = tmp_path / "work"
    cfg_path = tmp_path / "config.json"
    data_dir.mkdir(parents=True)
    sess_dir.mkdir(parents=True)
    work_dir.mkdir(parents=True)

    monkeypatch.setattr(config_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config_mod, "SESSION_DIR", str(sess_dir))
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(cfg_path))
    monkeypatch.setattr(tools, "DATA_DIR", str(data_dir))
    monkeypatch.setenv("LH_NO_TOAST", "1")
    cfg = config_mod.load()
    cfg["workspace"] = str(work_dir)
    config_mod.save(cfg)
    yield {"root": tmp_path, "data": data_dir, "work": work_dir, "cfg": cfg}


# ---------- 1. Reference Project & Training Studio 3.0 ----------
def test_reference_archive_unpacked_and_llama_tools_found():
    ref_dir = os.path.join(ROOT, "lora_train_reference")
    assert os.path.isdir(ref_dir)
    assert os.path.isfile(os.path.join(ref_dir, "Project_V1.0.py"))
    assert ut.find_llama_tool("llama-quantize") is not None
    assert cluster_mod.find_rpc_binary() is not None


def test_training_universal_peft_script_and_layer_inspector(isolated_env):
    script = ut.generate_unsloth_script({
        "backend": "peft_universal",
        "model": "Qwen/Qwen2.5-1.5B-Instruct",
        "method": "sft",
        "dataset_path": str(isolated_env["root"] / "train.jsonl"),
        "output_dir": str(isolated_env["root"] / "out_peft"),
        "lora_r": 16,
        "lora_alpha": 32,
        "max_steps": 30,
        "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj"],
        "export_gguf": True,
        "gguf_quant": "Q4_K_M",
    })
    compile(script, "train_peft_universal.py", "exec")
    assert "LoraConfig" in script
    assert "merge_and_unload" in script
    assert "-100" in script

    layers = ut.inspect_model_layers("unsloth/Qwen3-4B-bnb-4bit")
    assert layers["ok"] is True
    assert "q_proj" in layers["layers"]
    assert len(layers["recommended"]) >= 4

    # Local fake model config.json inspection
    fake_model_dir = isolated_env["root"] / "fake_phi"
    fake_model_dir.mkdir()
    (fake_model_dir / "config.json").write_text(json.dumps({"model_type": "phi3"}), encoding="utf-8")
    phi_layers = ut.inspect_model_layers(str(fake_model_dir))
    assert phi_layers["ok"] is True
    assert "qkv_proj" in phi_layers["layers"]


def test_training_speed_eta_metrics_parser():
    m = ut._parse_metrics_line(" 20/100 [00:40<02:40,  2.00s/it]")
    assert m["step"] == 20
    assert m["max_steps"] == 100
    assert m["speed_it_s"] == 0.5
    assert m["eta_min"] is not None


# ---------- 2. Dashboard 2.0 ----------
def test_dashboard_v2_counters_csv_and_processes():
    dashboard_mod._reset()
    assert dashboard_mod._MAX_POINTS >= 1800
    dashboard_mod.record_tokens(120, tps=42.5)
    dashboard_mod.record_tokens(80, tps=55.0)
    dashboard_mod.record_tool_call("web_search", ok=True)
    dashboard_mod.record_tool_call("shell", ok=False)

    dashboard_mod._collect_once()

    summary = dashboard_mod.get_summary()
    assert summary["total_tokens"] == 200
    assert summary["peak_tps"] == 55.0
    assert summary["last_tps"] == 55.0
    assert summary["tool_calls"] == 2
    assert summary["tool_errors"] == 1

    csv_text = dashboard_mod.export_csv(minutes=10)
    assert "gpu_temp" in csv_text
    assert "net_rx_kbps" in csv_text

    procs = dashboard_mod.get_top_processes(limit=5)
    assert procs["ok"] is True
    assert isinstance(procs["processes"], list)
    assert len(procs["processes"]) >= 1

    dashboard_mod.reset_counters()
    assert dashboard_mod.get_summary()["total_tokens"] == 0
    assert dashboard_mod.get_summary()["tool_calls"] == 0


# ---------- 3. Cluster 2.0 ----------
def test_cluster_v2_new_tasks_toggle_and_rpc():
    cluster_mod._reset()
    rag_res = cluster_mod.submit_task("rag_chunk_score", {
        "query": "lora quantization gguf",
        "chunks": [
            {"id": 1, "file": "a.md", "text": "Unrelated cooking recipe with tomatoes"},
            {"id": 2, "file": "b.md", "text": "Fast LoRA training and GGUF quantization pipeline"},
        ],
    })
    assert rag_res["ok"] is True
    assert rag_res["result"]["matches"][0]["id"] == 2

    mat_res = cluster_mod.submit_task("matrix_ops", {
        "query_vec": [1.0, 0.0, 0.0],
        "vectors": [[0.0, 1.0, 0.0], [0.9, 0.1, 0.0]],
        "top_k": 2,
    })
    assert mat_res["ok"] is True
    assert mat_res["result"]["top"][0]["index"] == 1
    assert mat_res["result"]["top"][0]["cosine"] > 0.95

    with cluster_mod._LOCK:
        cluster_mod._LLM_NODES["http://192.168.1.77:11434/v1"] = {
            "url": "http://192.168.1.77:11434/v1",
            "label": "Node77",
            "enabled": True,
            "alive": True,
            "models": ["llama3:8b"],
            "latency_ms": 12.0,
        }
    tog = cluster_mod.toggle_llm_node("192.168.1.77:11434", enabled=False)
    assert tog["ok"] is True and tog["node"]["enabled"] is False
    assert cluster_mod.pick_llm_node({"cluster_enabled": True, "cluster_offload_on_hot": True}, {"status": "hot"}) is None

    st = cluster_mod.status({})
    assert "rpc" in st
    assert st["rpc"]["available"] is True


# ---------- 4. Voice 2.0 (Edge Neural TTS & Sentence Splitter) ----------
def test_voice_v2_edge_neural_and_sentence_splitter(isolated_env):
    chunks = voice.split_sentences("Привет! Я голосовой ассистент LocalHarness. Чем могу помочь тебе сегодня?", max_chunk=45)
    assert len(chunks) >= 2

    presets = voice.list_presets(isolated_env["cfg"])
    assert len(presets) >= 6
    assert any(p.get("edge_voice") == "ru-RU-SvetlanaNeural" for p in presets)

    fake_mp3 = b"ID3" + b"\x00" * 256
    with mock.patch.object(voice, "_synthesize_edge_neural", return_value={
        "ok": True,
        "audio": fake_mp3,
        "mime": "audio/mpeg",
        "ext": "mp3",
        "engine_used": "edge_neural",
        "neural_voice": "ru-RU-SvetlanaNeural",
    }):
        res = voice.synthesize(
            "Проверка студийного нейронного голоса.",
            cfg=isolated_env["cfg"],
            engine="edge",
            speed=1.15,
            pitch=4,
            save_file=True,
        )
    assert res["ok"] is True
    assert res["engine_used"] == "edge_neural"
    assert res["audio_b64"].startswith("data:audio/mpeg;base64,")
    assert res["file"].endswith(".mp3")
    assert os.path.isfile(res["file"])


# ---------- 5. Plugins 2.0 & Author Plugin gguf_toolkit.py ----------
def _make_minimal_gguf_v3(path):
    """Build a valid binary GGUF v3 header with 2 KV pairs (architecture + context_length)."""
    def pack_str(s):
        b = s.encode("utf-8")
        return struct.pack("<Q", len(b)) + b

    buf = bytearray()
    buf += b"GGUF"
    buf += struct.pack("<I", 3)   # version 3
    buf += struct.pack("<Q", 42)  # tensor_count = 42
    buf += struct.pack("<Q", 2)   # kv_count = 2
    # KV 1: general.architecture = "qwen2" (type 8 = STRING)
    buf += pack_str("general.architecture") + struct.pack("<I", 8) + pack_str("qwen2")
    # KV 2: qwen2.context_length = 32768 (type 4 = UINT32)
    buf += pack_str("qwen2.context_length") + struct.pack("<I", 4) + struct.pack("<I", 32768)
    with open(path, "wb") as fh:
        fh.write(buf)


def test_plugins_v2_gguf_toolkit_and_settings(isolated_env):
    plugins.scan(force=True)
    assert "gguf_toolkit.py" in plugins.PLUGINS
    pinfo = plugins.PLUGINS["gguf_toolkit.py"]
    assert pinfo["error"] is None
    assert "gguf_inspect" in pinfo["tools"]
    assert len(pinfo.get("tool_specs") or []) == 3
    assert len(pinfo.get("settings_schema") or []) >= 2

    gguf_file = str(isolated_env["root"] / "test_model.gguf")
    _make_minimal_gguf_v3(gguf_file)

    insp = plugins.call("gguf_inspect", isolated_env["cfg"], {"gguf_path": gguf_file})
    assert insp["ok"] is True
    assert insp["gguf_version"] == 3
    assert insp["tensor_count"] == 42
    assert insp["architecture"] == "qwen2"
    assert insp["context_length"] == 32768

    sv = plugins.save_plugin_settings("gguf_toolkit.py", {"bench_threads": 8, "default_quant": "Q5_K_M"})
    assert sv["ok"] is True
    gt = plugins.get_plugin_settings("gguf_toolkit.py")
    assert gt["values"]["bench_threads"] == 8
    assert gt["values"]["default_quant"] == "Q5_K_M"


# ---------- 6. New HTTP Endpoints & WebUI Sync ----------
def test_new_http_endpoints_and_webui_sync(isolated_env):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        # /api/train/layers
        req = urllib.request.Request(
            base + "/api/train/layers",
            data=json.dumps({"model": "unsloth/Qwen3-4B-bnb-4bit"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        lr = json.loads(urllib.request.urlopen(req, timeout=8).read())
        assert lr["ok"] is True and "q_proj" in lr["layers"]

        # /api/dashboard/processes & /api/dashboard/export.csv
        pr = json.loads(urllib.request.urlopen(base + "/api/dashboard/processes", timeout=8).read())
        assert pr["ok"] is True and isinstance(pr["processes"], list)

        csv_resp = urllib.request.urlopen(base + "/api/dashboard/export.csv?minutes=5", timeout=8).read().decode("utf-8-sig")
        assert "gpu_temp" in csv_resp
    finally:
        srv.shutdown()
        srv.server_close()

    for fname in ("index.html", "style.css", "app.js", "worker.html"):
        p1 = os.path.join(ROOT, "webui", fname)
        p2 = os.path.join(ROOT, "harness", "webui", fname)
        with open(p1, "rb") as f1, open(p2, "rb") as f2:
            assert f1.read() == f2.read(), f"WebUI out of sync: {fname}"

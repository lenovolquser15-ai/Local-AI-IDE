import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from harness import config as config_mod
from harness import lms as lms_mod
from harness import server as server_mod
from harness import sizing as sizing_mod


def _make_fake_lmstudio_home(tmp_path, include_vulkan=True, active_name="llama.cpp-win-x86_64-nvidia-cuda12-avx2"):
    home = tmp_path / ".lmstudio"
    internal = home / ".internal"
    internal.mkdir(parents=True)

    engines = [
        {
            "manifest": {
                "name": "llama.cpp-win-x86_64-nvidia-cuda12-avx2",
                "version": "2.25.2",
                "supported_model_formats": ["gguf"],
                "gpu": {"make": "Nvidia", "framework": "CUDA"},
            },
            "displayData": [["en", {"displayName": "CUDA 12 llama.cpp (Windows)"}]],
            "backendCompatibility": {"status": "errorSurveyingHardware", "message": "GPU survey error"},
            "memoryInfo": {"vramCapacity": 0},
            "hardwareSurveyResult": {"gpuSurveyResult": {"gpuInfo": []}},
        },
        {
            "manifest": {
                "name": "llama.cpp-win-x86_64-avx2",
                "version": "2.25.2",
                "supported_model_formats": ["gguf"],
            },
            "displayData": [["en", {"displayName": "CPU llama.cpp (Windows)"}]],
            "backendCompatibility": {"status": "compatible"},
            "memoryInfo": {"vramCapacity": 0},
            "hardwareSurveyResult": {"gpuSurveyResult": {"gpuInfo": []}},
        },
    ]
    if include_vulkan:
        engines.append({
            "manifest": {
                "name": "llama.cpp-win-x86_64-vulkan-avx2",
                "version": "2.83.0",
                "supported_model_formats": ["gguf"],
                "gpu": {"framework": "Vulkan"},
            },
            "displayData": [["en", {"displayName": "Vulkan llama.cpp (Windows)"}]],
            "backendCompatibility": {"status": "compatible"},
            "memoryInfo": {"vramCapacity": int(7.71 * (1024 ** 3))},
            "hardwareSurveyResult": {
                "gpuSurveyResult": {"gpuInfo": [{"name": "NVIDIA GeForce RTX 5060 Laptop GPU"}]}
            },
        })

    (internal / "internal-engine-index.json").write_text(
        json.dumps({"json": engines}, ensure_ascii=False), encoding="utf-8"
    )
    (internal / "backend-preferences-v1.json").write_text(
        json.dumps([
            {"model_format": "mlx", "name": "mlx-engine", "version": "1.0.0"},
            {"model_format": "gguf", "name": active_name, "version": "2.25.2"},
        ]),
        encoding="utf-8",
    )
    return home


def test_list_and_select_vulkan_runtime(tmp_path, monkeypatch):
    home = _make_fake_lmstudio_home(tmp_path, include_vulkan=True)
    monkeypatch.setenv("LMSTUDIO_HOME", str(home))
    monkeypatch.setattr(lms_mod, "run_cli", lambda *a, **kw: ("", ""))

    info = lms_mod.list_runtimes({})
    assert info["ok"] is True
    assert info["vulkan_installed"] is True
    assert info["vulkan_active"] is False
    vk = next(r for r in info["runtimes"] if r["kind"] == "vulkan")
    assert vk["version"] == "2.83.0"
    assert vk["compatible"] is True
    assert vk["vram_gb"] == pytest.approx(7.71, abs=0.05)
    assert "RTX 5060" in vk["gpus"][0]

    # Switch to Vulkan via alias "vulkan"
    switched = lms_mod.select_runtime("vulkan", cfg={})
    assert switched["ok"] is True
    assert switched["vulkan_active"] is True
    assert switched["active_name"] == lms_mod.default_vulkan_runtime_name()

    # Ensure non-gguf entries in backend-preferences-v1.json were preserved
    prefs = json.loads((home / ".internal" / "backend-preferences-v1.json").read_text(encoding="utf-8"))
    assert any(p.get("model_format") == "mlx" for p in prefs)
    gguf_pref = next(p for p in prefs if p.get("model_format") == "gguf")
    assert gguf_pref["name"] == lms_mod.default_vulkan_runtime_name()
    assert gguf_pref["version"] == "2.83.0"


def test_vulkan_offered_for_install_when_missing(tmp_path, monkeypatch):
    home = _make_fake_lmstudio_home(tmp_path, include_vulkan=False)
    monkeypatch.setenv("LMSTUDIO_HOME", str(home))

    info = lms_mod.list_runtimes({})
    assert info["vulkan_installed"] is False
    vk = next(r for r in info["runtimes"] if r["kind"] == "vulkan")
    assert vk["installed"] is False

    res = lms_mod.select_runtime("vulkan", cfg={})
    assert res["ok"] is False
    assert res.get("need_install") is True

    args = lms_mod.install_or_update_runtime_args("--malicious-flag")
    assert args == ["runtime", "get", lms_mod.default_vulkan_runtime_name(), "-y"]


def test_gpu_offload_and_cli_args_and_estimate_parser():
    assert lms_mod.normalize_gpu_offload("max") == ("max", 1.0)
    assert lms_mod.normalize_gpu_offload("off") == ("off", 0.0)
    assert lms_mod.normalize_gpu_offload("75%") == ("0.75", 0.75)
    assert lms_mod.normalize_gpu_offload(50) == ("0.5", 0.5)
    assert lms_mod.normalize_gpu_offload(0.35) == ("0.35", 0.35)
    assert lms_mod.normalize_gpu_offload(False) == ("off", 0.0)

    args = lms_mod.build_load_cli_args("qwen3-vl-2b", context_length=32768, gpu="0.75", parallel=3, ttl=600)
    assert args == [
        "load", "qwen3-vl-2b", "--context-length", "32768",
        "--gpu", "0.75", "-y", "--parallel", "3", "--ttl", "600",
    ]

    sample_cli = (
        "Model: huihui-qwen3-vl-2b-instruct-abliterated\n"
        "Context Length: 16\xa0384\n"
        "GPU Offload: 100%\n"
        "Estimated GPU Memory:   3.48 GiB\n"
        "Estimated Total Memory: 3.48 GiB\n"
        "Estimate: This model may be loaded based on your resource guardrails settings.\n"
    )
    parsed = lms_mod.parse_estimate_cli_output(sample_cli)
    assert parsed is not None
    assert parsed["gpu_memory_gb"] == 3.48
    assert parsed["total_memory_gb"] == 3.48
    assert parsed["gpu_offload_text"] == "100%"


def test_model_load_preset_roundtrip_and_kv_quant(tmp_path, monkeypatch):
    home = _make_fake_lmstudio_home(tmp_path, include_vulkan=True)
    monkeypatch.setenv("LMSTUDIO_HOME", str(home))
    fake_disk = [{
        "modelKey": "qwen3-vl-2b",
        "path": "noctrex/Qwen3-VL-2B/Qwen3-VL-2B-Q4_K_S.gguf",
        "maxContextLength": 262144,
    }]
    monkeypatch.setattr(lms_mod, "disk_models", lambda *a, **kw: (fake_disk, ""))

    cfg_path = home / ".internal" / "user-concrete-model-default-config" / "noctrex" / "Qwen3-VL-2B" / "Qwen3-VL-2B-Q4_K_S.gguf.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({
        "preset": "my-preset",
        "operation": {"fields": [{"key": "llm.prediction.temperature", "value": 0.6}]},
        "load": {"fields": [{"key": "llm.load.llama.cpuThreadPoolSize", "value": 8}]},
    }), encoding="utf-8")

    ok = lms_mod.apply_model_load_preset(
        "qwen3-vl-2b",
        context_length=49152,
        gpu_offload="0.8",
        flash_attention=True,
        kv_quant="q8_0",
        parallel=2,
        cfg={},
    )
    assert ok is True

    preset = lms_mod.read_model_load_preset("qwen3-vl-2b", cfg={})
    assert preset["context_length"] == 49152
    assert preset["gpu_offload"] == "0.8"
    assert preset["flash_attention"] is True
    assert preset["kv_quant"] == "q8_0"
    assert preset["parallel"] == 2

    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert raw["preset"] == "my-preset"
    assert raw["operation"]["fields"][0]["key"] == "llm.prediction.temperature"
    load_keys = {f["key"]: f["value"] for f in raw["load"]["fields"]}
    assert load_keys["llm.load.llama.cpuThreadPoolSize"] == 8
    assert load_keys["llm.load.llama.kCacheQuantizationType"] == {"checked": True, "value": "q8_0"}


def test_sizing_supports_256k_context_and_kv_quant():
    hw = {
        "ram_total_gb": 32.0,
        "ram_free_gb": 24.0,
        "has_gpu": True,
        "gpu_name": "RTX 5060",
        "vram_total_gb": 8.0,
        "vram_free_gb": 7.5,
        "gpu_bandwidth_gbps": 320.0,
        "cpu_bandwidth_gbps": 45.0,
    }
    model = {"modelKey": "qwen3-4b", "paramsString": "4B", "sizeBytes": int(2.5 * (1024 ** 3))}
    est_fp16 = sizing_mod.estimate_model_fit(model, context_length=200000, hw=hw, gpu_offload="max", kv_quant="f16")
    est_q4 = sizing_mod.estimate_model_fit(model, context_length=200000, hw=hw, gpu_offload="max", kv_quant="q4_0")
    est_cpu = sizing_mod.estimate_model_fit(model, context_length=8192, hw=hw, gpu_offload="off", kv_quant="f16")

    assert est_fp16["context_length"] == 200000
    assert est_q4["kv_cache_gb"] < est_fp16["kv_cache_gb"]
    assert est_cpu["fit"] == "cpu_only"
    assert est_cpu["offload_pct"] == 0


def test_http_lms_load_and_runtime_endpoints(tmp_path, monkeypatch):
    home = _make_fake_lmstudio_home(tmp_path, include_vulkan=True)
    monkeypatch.setenv("LMSTUDIO_HOME", str(home))
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("LH_CONFIG", str(cfg_file))
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(cfg_file))

    fake_disk = [{
        "type": "llm",
        "modelKey": "huihui-qwen3-vl-2b",
        "path": "noctrex/Huihui-Qwen3-VL-2B.gguf",
        "displayName": "Huihui Qwen3 VL 2B",
        "maxContextLength": 65536,
        "sizeBytes": int(2.4 * (1024 ** 3)),
        "paramsString": "2B",
        "vision": True,
        "trainedForToolUse": True,
    }]
    monkeypatch.setattr(lms_mod, "disk_models", lambda *a, **kw: (fake_disk, ""))
    monkeypatch.setattr(lms_mod, "loaded_models", lambda *a, **kw: ([], ""))
    monkeypatch.setattr(lms_mod, "run_cli", lambda *a, **kw: ("", ""))
    monkeypatch.setattr(lms_mod, "server_start", lambda *a, **kw: (True, ""))

    captured_jobs = []

    def fake_start_job(kind, args, timeout=3600, after_ok=None):
        captured_jobs.append((kind, list(args)))
        return {"ok": True, "job": {"id": "load-1", "kind": kind, "running": False, "rc": 0, "lines": []}}

    monkeypatch.setattr(server_mod, "start_lms_job", fake_start_job)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(base + "/api/lms/runtimes", timeout=5) as r:
            rt_data = json.loads(r.read().decode("utf-8"))
        assert rt_data["vulkan_installed"] is True

        req = urllib.request.Request(
            base + "/api/lms/load",
            data=json.dumps({
                "model": "huihui-qwen3-vl-2b",
                "runtime": "vulkan",
                "context_length": 100000,  # Exceeds maxContextLength 65536 -> should clamp cleanly!
                "gpu": "0.85",
                "kv_quant": "q8_0",
                "flash_attention": True,
                "parallel": 2,
                "select_for_chat": True,
                "ensure_server": False,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            load_res = json.loads(r.read().decode("utf-8"))

        assert load_res["ok"] is True
        assert load_res["context_length"] == 65536
        assert load_res["gpu"] == "0.85"
        assert any("65536" in w for w in load_res.get("warnings", []))
        assert captured_jobs == [(
            "load",
            ["load", "huihui-qwen3-vl-2b", "--context-length", "65536", "--gpu", "0.85", "-y", "--parallel", "2"],
        )]

        saved_cfg = config_mod.load()
        assert saved_cfg["model"] == "huihui-qwen3-vl-2b"
        assert saved_cfg["lms_context_length"] == 65536
        assert saved_cfg["context_tokens"] == 65536
        assert lms_mod.list_runtimes({})["vulkan_active"] is True

        # Test 1-click Vulkan GPU + CPU <= 50% preset endpoint
        captured_jobs.clear()
        vk_req = urllib.request.Request(
            base + "/api/lms/vulkan_gpu_preset",
            data=json.dumps({"model": "huihui-qwen3-vl-2b", "cpu_cap_pct": 50}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(vk_req, timeout=5) as r:
            vk_res = json.loads(r.read().decode("utf-8"))
        assert vk_res["ok"] is True
        assert vk_res["gpu"] == "max"
        assert vk_res["cpu_cap_pct"] == 50
        assert 2 <= vk_res["cpu_threads"] <= 4
        assert vk_res["context_length"] == 65536  # Preserves <= 64K user context without silent downgrade
        assert captured_jobs and captured_jobs[0][1][:6] == [
            "load", "huihui-qwen3-vl-2b", "--context-length", "65536", "--gpu", "max"
        ]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_auto_reload_model_when_context_increased(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    monkeypatch.setenv("LH_CONFIG", str(cfg_file))
    monkeypatch.setattr(config_mod, "CONFIG_PATH", str(cfg_file))
    config_mod.save({
        **config_mod.DEFAULTS,
        "provider": "lmstudio",
        "model": "huihui-qwen3-vl-2b",
        "context_tokens": 8192,
        "lms_context_length": 8192,
        "lms_gpu_offload": "max",
    })

    loaded_state = [{
        "type": "llm",
        "identifier": "huihui-qwen3-vl-2b",
        "modelKey": "huihui-qwen3-vl-2b",
        "contextLength": 8192,
        "maxContextLength": 65536,
    }]
    monkeypatch.setattr(lms_mod, "loaded_models", lambda *a, **kw: (list(loaded_state), ""))
    unloads = []
    monkeypatch.setattr(lms_mod, "run_cli", lambda args, *a, **kw: (unloads.append(list(args)) or "", ""))
    jobs = []

    def fake_start_job(kind, args, timeout=3600, after_ok=None):
        jobs.append((kind, list(args), after_ok is not None))
        return {"ok": True, "job": {"id": "reload-1", "kind": kind, "running": False, "rc": 0, "lines": []}}

    monkeypatch.setattr(server_mod, "start_lms_job", fake_start_job)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        # 1. Increasing context 8192 -> 32768 must auto-unload & reload the loaded model!
        req = urllib.request.Request(
            base + "/api/config",
            data=json.dumps({"config": {"context_tokens": 32768}}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            res = json.loads(r.read().decode("utf-8"))
        assert res["ok"] is True
        assert res["reloading_model"]["reloading"] is True
        assert res["reloading_model"]["from_ctx"] == 8192
        assert res["reloading_model"]["to_ctx"] == 32768
        assert unloads == [["unload", "huihui-qwen3-vl-2b"]]
        assert jobs and jobs[0][0] == "load"
        assert jobs[0][1][:6] == ["load", "huihui-qwen3-vl-2b", "--context-length", "32768", "--gpu", "max"]
        assert jobs[0][2] is True  # after_ok CPU cap callback attached

        # 2. Shrinking context without reload_on_shrink should NOT trigger unnecessary reload
        unloads.clear()
        jobs.clear()
        loaded_state[0]["contextLength"] = 32768
        req2 = urllib.request.Request(
            base + "/api/config",
            data=json.dumps({"config": {"lms_context_length": 16384}}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req2, timeout=5) as r:
            res2 = json.loads(r.read().decode("utf-8"))
        assert res2["ok"] is True
        assert "reloading_model" not in res2
        assert jobs == []

        # 3. Shrinking context WITH reload_on_shrink=True SHOULD reload
        req3 = urllib.request.Request(
            base + "/api/config",
            data=json.dumps({"config": {"lms_context_length": 8192}, "reload_on_shrink": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req3, timeout=5) as r:
            res3 = json.loads(r.read().decode("utf-8"))
        assert res3["ok"] is True
        assert res3["reloading_model"]["reloading"] is True
        assert res3["reloading_model"]["to_ctx"] == 8192
    finally:
        httpd.shutdown()
        httpd.server_close()

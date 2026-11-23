"""Tests for Unsloth Fine-Tuning Studio (harness.unsloth_trainer + tools + server + UI)."""
import json
import os
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from harness import config as config_mod
from harness import server, session, tools, unsloth_trainer as ut


@pytest.fixture
def isolated_train_env(tmp_path, monkeypatch):
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
    monkeypatch.setattr(session, "SESSION_DIR", str(sess_dir))
    monkeypatch.setattr(tools, "DATA_DIR", str(data_dir))
    cfg = config_mod.load()
    cfg["workspace"] = str(work_dir)
    config_mod.save(cfg)
    yield {"root": tmp_path, "data": data_dir, "cfg": cfg}


def test_dataset_export_from_sessions_and_append(isolated_train_env):
    sid = session.create("Fine-tune seed")
    session.message(sid, "user", "Как оптимизировать SQL запрос в PostgreSQL?")
    session.message(sid, "assistant", "Используйте EXPLAIN ANALYZE и составные индексы по полям WHERE и JOIN.")
    session.message(sid, "user", "Сбойный запрос")
    session.message(sid, "assistant", "Модель не ответила: timeout")

    res = ut.export_sessions_to_dataset("my_chats.jsonl")
    assert res["ok"] is True
    assert res["count"] == 1
    assert os.path.isfile(res["path"])

    add_res = ut.append_dataset_example("my_chats.jsonl", "Объясни LoRA", "LoRA обучает низкоранговые матрицы адаптеров.")
    assert add_res["ok"] is True

    listing = ut.list_datasets()
    assert listing["ok"] is True
    ds = next(d for d in listing["datasets"] if d["name"] == "my_chats.jsonl")
    assert ds["rows"] == 2


@pytest.mark.parametrize("method", ["sft", "grpo", "dpo", "vision"])
def test_generate_unsloth_scripts_compile_cleanly(isolated_train_env, method):
    script = ut.generate_unsloth_script({
        "model": "unsloth/Qwen3-4B-bnb-4bit",
        "method": method,
        "dataset_path": "hf://IlyaGusev/ru_turbo_alpaca" if method == "sft" else "C:/data/train.jsonl",
        "output_dir": str(isolated_train_env["root"] / "out"),
        "lora_r": 16,
        "max_steps": 40,
        "chat_template": "qwen-2.5",
        "use_rslora": True,
        "packing": True,
        "export_gguf": True,
        "gguf_quant": "q4_k_m",
    })
    compile(script, f"train_{method}.py", "exec")
    if method == "vision":
        assert "FastVisionModel.from_pretrained" in script
    else:
        assert "FastLanguageModel.from_pretrained" in script
        assert "save_pretrained_gguf" in script
    if method == "sft":
        assert "SFTTrainer" in script
        assert "IlyaGusev/ru_turbo_alpaca" in script
    elif method == "grpo":
        assert "GRPOTrainer" in script
    elif method == "dpo":
        assert "DPOTrainer" in script


def test_import_csv_preview_delete_and_autotune(isolated_train_env):
    csv_text = "instruction,input,output\nКак дела?,контекст,Отлично!\nЧто такое Python?,,Язык программирования.\n"
    imp = ut.import_dataset_file("qa.csv", csv_text)
    assert imp["ok"] is True and imp["count"] == 2

    pv = ut.preview_dataset("qa.jsonl")
    assert pv["ok"] is True and pv["total"] == 2 and pv["samples"][0]["instruction"] == "Как дела?"

    tune = ut.autotune_for_gpu("unsloth/Qwen3-4B-bnb-4bit", "sft")
    assert tune["ok"] is True
    assert tune["recommended"]["batch_size"] >= 1
    assert tune["estimate"]["estimated_vram_gb"] > 0

    rm = ut.delete_dataset("qa.jsonl")
    assert rm["ok"] is True and rm["removed"] == "qa.jsonl"


def test_synthetic_dataset_generator_mocked_llm(isolated_train_env, monkeypatch):
    from harness import llm as llm_mod

    class _FakeLLM:
        def __init__(self, cfg):
            pass

        def chat(self, messages, **kw):
            return {
                "content": json.dumps([
                    {"instruction": "Что такое FastAPI?", "input": "", "output": "Современный асинхронный веб-фреймворк на Python."},
                    {"instruction": "Зачем нужен Pydantic?", "input": "", "output": "Для валидации и сериализации данных."},
                ], ensure_ascii=False)
            }

    monkeypatch.setattr(llm_mod, "LLM", _FakeLLM)
    res = ut.generate_synthetic_dataset("FastAPI", count=2, dataset_name="synth.jsonl", cfg=isolated_train_env["cfg"])
    assert res["ok"] is True and res["added"] == 2
    pv = ut.preview_dataset("synth.jsonl")
    assert pv["total"] == 2


def test_parse_trl_metrics_line():
    m = ut._parse_metrics_line("{'loss': 1.2345, 'learning_rate': 0.00018, 'epoch': 0.52}")
    assert m["loss"] == 1.2345
    assert m["lr"] == 0.00018
    assert m["epoch"] == 0.52

    m2 = ut._parse_metrics_line(" 15/60 [00:42<02:06,  2.81s/it]")
    assert m2["step"] == 15 and m2["max_steps"] == 60


def test_dry_run_and_agent_tools(isolated_train_env):
    ut.append_dataset_example("demo.jsonl", "Напиши привет", "Привет! Чем помочь?")
    ds_path = os.path.join(ut.datasets_dir(), "demo.jsonl")

    dry = ut.start_training({"model": "unsloth/Qwen3-4B-bnb-4bit", "dataset_path": ds_path, "method": "sft", "dry_run": True})
    assert dry["ok"] is True
    assert dry["dry_run"] is True
    assert os.path.isfile(dry["script_path"])

    tk = tools.Toolkit(isolated_train_env["cfg"])
    r_ds = tk.call("unsloth_train", {"action": "datasets"})
    assert r_ds["ok"] is True and any(d["name"] == "demo.jsonl" for d in r_ds["datasets"])
    r_sc = tk.call("unsloth_train", {"action": "script", "method": "grpo", "model": "unsloth/Qwen3-4B-bnb-4bit"})
    assert r_sc["ok"] is True and "GRPOTrainer" in r_sc["script"]


def test_server_train_endpoints_and_ui(isolated_train_env):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        st = json.loads(urllib.request.urlopen(base + "/api/train/status", timeout=8).read())
        assert st["ok"] is True and len(st["presets"]) >= 5

        req = urllib.request.Request(
            base + "/api/train/dataset/append",
            data=json.dumps({"name": "api_ds.jsonl", "instruction": "Q", "output": "A"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        add_r = json.loads(urllib.request.urlopen(req, timeout=8).read())
        assert add_r["ok"] is True

        req2 = urllib.request.Request(
            base + "/api/train/script",
            data=json.dumps({"model": "unsloth/Qwen3-4B-bnb-4bit", "method": "sft"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        sc_r = json.loads(urllib.request.urlopen(req2, timeout=8).read())
        assert sc_r["ok"] is True and "FastLanguageModel" in sc_r["script"]
    finally:
        srv.shutdown()
        srv.server_close()

    for folder in ("webui", os.path.join("harness", "webui")):
        html = open(os.path.join(config_mod.ROOT, folder, "index.html"), encoding="utf-8").read()
        js = open(os.path.join(config_mod.ROOT, folder, "app.js"), encoding="utf-8").read()
        assert 'id="viewTrain"' in html
        assert 'id="tabTrain"' in html
        assert "refreshTrain" in js

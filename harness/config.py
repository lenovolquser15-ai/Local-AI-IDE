import copy
import json
import os
import sys
import threading

DEFAULTS = {
    "port": 7788,
    "host": "127.0.0.1",
    "provider": "lmstudio",
    "model": None,
    "tool_protocol": "auto",
    "providers": {
        "lmstudio": {
            "label": "LM Studio",
            "base_url": "http://127.0.0.1:1234/v1",
            "api_key": "lm-studio",
            "autostart": True,
            "exe": os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "LM Studio", "LM Studio.exe"),
            "lms_cli": os.path.join(os.path.expanduser("~"), ".lmstudio", "bin", "lms.exe"),
        },
        "ollama": {
            "label": "Ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "api_key": "ollama",
            "autostart": False,
        },
        "strata": {
            "label": "Strata (MoE Engine)",
            "base_url": "http://127.0.0.1:8080/v1",
            "api_key": "strata",
            "autostart": False,
        },
    },
    "workspace": "",
    "workspace_enabled": False,
    "workspaces": [],
    "permissions": {"mode": "auto"},
    "max_steps": 40,
    "goal_rounds": 6,
    "context_tokens": 16384,
    "context_mode": "auto",
    "lms_context_length": 16384,
    "lms_gpu_offload": "max",
    "lms_parallel": 1,
    "lms_flash_attention": True,
    "lms_kv_quant": "f16",
    "lms_eval_batch": 512,
    "lms_cpu_threads": 4,
    "lms_cpu_cap_pct": 50,
    "lms_runtime": "",
    "lms_ttl": 0,
    "tool_profile": "full",
    "system_prompt": "",
    "ssh_profiles": {},
    "ssh_profile": "",
    "plugins_disabled": [],
    "plugin_catalog_url": "",
    "temperature": 0.4,
    "max_tokens": 1500,
    "search_engine": "auto",
    "search_searxng": False,
    "search_searxng_url": "http://127.0.0.1:8080",
    "search_obscura": False,
    "obscura_path": "",
    "obscura_cdp": "http://127.0.0.1:9223",
    "obscura_stealth": True,
    "chrome_cdp": "http://127.0.0.1:9222",
    "memory_enabled": False,
    "image_gen_enabled": False,
    "comfyui_url": "http://127.0.0.1:8188",
    "lan_access": False,
    "mobile_files": {"root": "", "read": True, "write": True, "ttl_hours": 24},
    "game_failsafe": True,
    "virustotal_api_key": "",
    "gpu_thermal_enabled": True,
    "gpu_temp_warn": 80,
    "gpu_temp_throttle": 85,
    "gpu_temp_critical": 95,
    "gpu_cooldown_duration": 180,
    "gpu_target_temp": 70,
    "cluster_enabled": True,
    "cluster_offload_on_hot": True,
    "cluster_llm_nodes": [],
    "theme": "dark",
    "persona": "default",
    "toast_notifications": True,
    "rag_auto_context": True,
    "rag_embedding_model": "",
    "mcp_servers": [],
    "voice_engine": "auto",
    "voice_xtts_url": "http://127.0.0.1:8020",
    "voice_preset": "ru_female_warm",
    "voice_speaker_wav": "",
    "voice_language": "ru",
    "voice_speed": 1.0,
    "voice_pitch": 0,
    "voice_auto_speak": False,
    "plugin_settings": {},
    "remote": {
        "auto_start": False,
        "code_ttl": 28800,
        "worker_url": "",
        "site_url": "",
        "secret": "",
        "epoch": 1,
        "cloudflared": "",
    },
}

LOCK = threading.RLock()

CONTEXT_MIN = 512
CONTEXT_MAX = 262144
MAX_TOKENS_MIN = 64
MAX_TOKENS_MAX = 32768


def as_int(value, default=None, lo=None, hi=None):
    """Приводит значение к int, не падая на мусоре; опционально зажимает в диапазон."""
    try:
        n = int(float(str(value).strip()))
    except (TypeError, ValueError, OverflowError):
        return default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def normalize_context(value, default=8192):
    """Размер контекста: только 512…262144, всё остальное — безопасный дефолт или граница."""
    fallback = as_int(default, 8192, CONTEXT_MIN, CONTEXT_MAX)
    return as_int(value, fallback, CONTEXT_MIN, CONTEXT_MAX)


def app_root():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROOT = app_root()


def data_dir():
    if DATA_DIR != os.path.join(ROOT, 'data'):
        return DATA_DIR
    return os.environ.get('LH_DATA') or DATA_DIR


def session_dir():
    if SESSION_DIR != os.path.join(ROOT, 'data', 'sessions'):
        return SESSION_DIR
    if os.environ.get('LH_DATA'):
        return os.path.join(os.environ['LH_DATA'], 'sessions')
    return SESSION_DIR


def config_path():
    if CONFIG_PATH != os.path.join(ROOT, 'config.json'):
        return CONFIG_PATH
    return os.environ.get('LH_CONFIG') or CONFIG_PATH


DATA_DIR = os.environ.get('LH_DATA') or os.path.join(ROOT, 'data')
SESSION_DIR = os.path.join(DATA_DIR, 'sessions')
CONFIG_PATH = os.environ.get('LH_CONFIG') or os.path.join(ROOT, 'config.json')

def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out

def load():
    d_dir = data_dir()
    s_dir = session_dir()
    c_path = config_path()
    os.makedirs(d_dir, exist_ok=True)
    os.makedirs(s_dir, exist_ok=True)
    raw = {}
    if os.path.exists(c_path):
        try:
            with open(c_path, 'r', encoding='utf-8') as fh:
                raw = json.load(fh)
        except Exception:
            raw = {}
    cfg = _merge(DEFAULTS, raw)
    for key in ("context_tokens", "lms_context_length"):
        cfg[key] = normalize_context(cfg.get(key), DEFAULTS[key])
    cfg["max_tokens"] = as_int(cfg.get("max_tokens"), DEFAULTS["max_tokens"], MAX_TOKENS_MIN, MAX_TOKENS_MAX)
    workspace = cfg.get('workspace') or os.path.join(ROOT, 'work')
    cfg['workspace'] = workspace
    os.makedirs(workspace, exist_ok=True)
    return cfg

def save(cfg):
    c_path = config_path()
    os.makedirs(os.path.dirname(os.path.abspath(c_path)), exist_ok=True)
    with LOCK:
        with open(c_path, 'w', encoding='utf-8') as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)


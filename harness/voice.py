"""Voice Engine for LocalHarness: XTTS v2 (zero-shot voice cloning & multilingual TTS),
OpenAI-compatible TTS endpoint support, and offline Windows SAPI5 WAV fallback."""
import base64
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.request

from . import config as config_mod

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_LOCK = threading.RLock()

BUILTIN_PRESETS = [
    {
        "id": "ru_female_warm",
        "name": "Алиса (Тёплый женский)",
        "lang": "ru",
        "gender": "female",
        "builtin": True,
        "desc": "Мягкий и естественный женский голос для диалогов и звонков",
    },
    {
        "id": "ru_male_calm",
        "name": "Максим (Спокойный мужской)",
        "lang": "ru",
        "gender": "male",
        "builtin": True,
        "desc": "Уверенный мужской баритон для технической озвучки и кода",
    },
    {
        "id": "en_nova",
        "name": "Nova (Multilingual XTTS v2)",
        "lang": "en",
        "gender": "female",
        "builtin": True,
        "desc": "Мультиязычный голос XTTS v2 (RU / EN / DE / FR / ES / ZH)",
    },
]


def voices_dir():
    d = os.path.join(config_mod.data_dir(), "voices")
    os.makedirs(d, exist_ok=True)
    return d


def out_dir():
    d = os.path.join(config_mod.data_dir(), "voice_out")
    os.makedirs(d, exist_ok=True)
    return d


def clean_speech_text(text, max_chars=1800):
    """Strip markdown, code blocks, URLs, and noisy punctuation for natural TTS reading."""
    s = str(text or "")
    s = re.sub(r"```[\s\S]*?```", " [блок кода] ", s)
    s = re.sub(r"`([^`]{1,80})`", r"\1", s)
    s = re.sub(r"https?://\S+", " ссылка ", s)
    s = re.sub(r"[*_~]+", "", s)
    s = re.sub(r"[#>|]+", " ", s)
    s = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_chars]


def list_presets(cfg=None):
    """Return built-in voice presets plus user-uploaded .wav samples for XTTS v2 cloning."""
    vdir = voices_dir()
    custom = []
    try:
        for fname in sorted(os.listdir(vdir)):
            if not fname.lower().endswith(".wav"):
                continue
            full = os.path.join(vdir, fname)
            stem = os.path.splitext(fname)[0]
            try:
                size_kb = round(os.path.getsize(full) / 1024.0, 1)
            except OSError:
                size_kb = 0
            custom.append({
                "id": "sample:" + fname,
                "name": "🎙️ " + stem + " (Клон XTTS v2)",
                "file": fname,
                "path": full,
                "size_kb": size_kb,
                "lang": "ru",
                "builtin": False,
                "desc": "Пользовательский WAV-образец для клонирования голоса (%s КБ)" % size_kb,
            })
    except OSError:
        pass
    return BUILTIN_PRESETS + custom


def save_sample(name, data_b64):
    """Save a user WAV sample for XTTS v2 zero-shot voice cloning."""
    clean_name = re.sub(r"[^A-Za-zА-Яа-яЁё0-9_\- ]", "", str(name or "").strip())[:40].strip()
    if not clean_name:
        clean_name = "voice_" + time.strftime("%H%M%S")
    raw_b64 = str(data_b64 or "").strip()
    if "," in raw_b64 and raw_b64.startswith("data:"):
        raw_b64 = raw_b64.split(",", 1)[1]
    try:
        raw = base64.b64decode(raw_b64)
    except Exception as exc:
        return {"ok": False, "error": "Некорректный base64 аудиофайла: %s" % exc}
    if len(raw) < 64:
        return {"ok": False, "error": "Аудиофайл слишком мал"}
    if len(raw) > 25 * 1024 * 1024:
        return {"ok": False, "error": "Аудиофайл превышает 25 МБ"}
    fname = clean_name.replace(" ", "_") + ".wav"
    path = os.path.join(voices_dir(), fname)
    with open(path, "wb") as fh:
        fh.write(raw)
    return {
        "ok": True,
        "id": "sample:" + fname,
        "file": fname,
        "path": path,
        "size_kb": round(len(raw) / 1024.0, 1),
        "presets": list_presets(),
    }


def delete_sample(sample_id_or_file):
    key = str(sample_id_or_file or "").strip()
    if key.startswith("sample:"):
        key = key[7:]
    fname = os.path.basename(key)
    if not fname:
        return {"ok": False, "error": "Не указан файл образца"}
    path = os.path.join(voices_dir(), fname)
    if not os.path.isfile(path):
        return {"ok": False, "error": "Образец не найден: %s" % fname}
    try:
        os.remove(path)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "removed": fname, "presets": list_presets()}


def check_xtts_server(url, timeout=2.0):
    base = str(url or "http://127.0.0.1:8020").rstrip("/")
    for probe in ("/speakers_list", "/speakers", "/docs", "/v1/models", "/"):
        try:
            req = urllib.request.Request(base + probe, headers={"User-Agent": "LocalHarness-Voice"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status < 500:
                    return {"alive": True, "url": base, "endpoint": probe}
        except Exception:
            continue
    return {"alive": False, "url": base}


def status(cfg=None):
    cfg = config_mod.load() if cfg is None else cfg
    xtts_url = cfg.get("voice_xtts_url") or "http://127.0.0.1:8020"
    probe = check_xtts_server(xtts_url, timeout=1.5)
    presets = list_presets(cfg)
    return {
        "ok": True,
        "engine": cfg.get("voice_engine", "auto"),
        "xtts_url": xtts_url,
        "xtts_alive": probe["alive"],
        "voice_preset": cfg.get("voice_preset", "ru_female_warm"),
        "voice_language": cfg.get("voice_language", "ru"),
        "voice_speed": float(cfg.get("voice_speed") or 1.0),
        "voice_auto_speak": bool(cfg.get("voice_auto_speak", False)),
        "sapi_available": os.name == "nt",
        "presets": presets,
    }


def _resolve_speaker_wav(voice_id, cfg=None):
    vid = str(voice_id or "").strip()
    if vid.startswith("sample:"):
        fname = os.path.basename(vid[7:])
        cand = os.path.join(voices_dir(), fname)
        if os.path.isfile(cand):
            return cand
    if vid and os.path.isfile(vid):
        return vid
    custom_cfg = (cfg or {}).get("voice_speaker_wav") or ""
    if custom_cfg and os.path.isfile(custom_cfg):
        return custom_cfg
    # If any sample exists in voices_dir, use the first one as XTTS reference
    vdir = voices_dir()
    try:
        wavs = [os.path.join(vdir, f) for f in sorted(os.listdir(vdir)) if f.lower().endswith(".wav")]
        if wavs:
            return wavs[0]
    except OSError:
        pass
    return vid or "default"


def _synthesize_xtts_http(text, xtts_url, speaker_wav, language="ru", speed=1.0, timeout=45.0):
    """Call local XTTS v2 server (xtts-api-server / Coqui server / OpenAI-compatible /v1/audio/speech)."""
    base = str(xtts_url or "http://127.0.0.1:8020").rstrip("/")
    # 1. Try xtts-api-server POST /tts_to_audio/
    payloads = [
        (
            base + "/tts_to_audio/",
            {"text": text, "speaker_wav": speaker_wav, "language": language, "speed": float(speed or 1.0)},
        ),
        (
            base + "/api/tts",
            {"text": text, "speaker_id": speaker_wav, "language_id": language},
        ),
        (
            base + "/v1/audio/speech",
            {"model": "xtts-v2", "input": text, "voice": os.path.basename(speaker_wav) or "alloy", "speed": float(speed or 1.0)},
        ),
    ]
    last_err = ""
    for endpoint, body_dict in payloads:
        try:
            body = json.dumps(body_dict, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                endpoint,
                data=body,
                headers={"Content-Type": "application/json", "User-Agent": "LocalHarness-XTTS"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                audio = resp.read()
                if audio and len(audio) > 100:
                    return {"ok": True, "audio": audio, "engine_used": "xtts_v2"}
        except Exception as exc:
            last_err = str(exc)
    return {"ok": False, "error": last_err or "XTTS v2 сервер не ответил"}


def _synthesize_sapi_wav(text, voice_id="ru_female_warm", speed=1.0):
    """Offline Windows SAPI5 synthesis to WAV bytes via System.Speech.Synthesis."""
    if os.name != "nt":
        return {"ok": False, "error": "Windows SAPI5 доступен только на Windows"}
    rate = int(max(-5, min(5, round((float(speed or 1.0) - 1.0) * 6))))
    gender_hint = "Male" if "male" in str(voice_id).lower() else "Female"
    tmp_wav = os.path.join(tempfile.gettempdir(), "lh_tts_%d_%d.wav" % (os.getpid(), int(time.time() * 1000)))
    safe_text = str(text).replace("'", "''").replace("\r", " ").replace("\n", " ")
    safe_path = tmp_wav.replace("'", "''")
    ps_script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate = %d; "
        "try { $s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::%s) } catch {}; "
        "$s.SetOutputToWaveFile('%s'); "
        "$s.Speak('%s'); "
        "$s.Dispose()"
    ) % (rate, gender_hint, safe_path, safe_text)
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps_script],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            creationflags=NO_WINDOW,
        )
        if os.path.isfile(tmp_wav) and os.path.getsize(tmp_wav) > 44:
            with open(tmp_wav, "rb") as fh:
                audio = fh.read()
            try:
                os.remove(tmp_wav)
            except OSError:
                pass
            return {"ok": True, "audio": audio, "engine_used": "windows_sapi5"}
    except Exception as exc:
        return {"ok": False, "error": "SAPI5 ошибка: %s" % exc}
    finally:
        if os.path.exists(tmp_wav):
            try:
                os.remove(tmp_wav)
            except OSError:
                pass
    return {"ok": False, "error": "Не удалось синтезировать WAV через Windows SAPI5"}


def synthesize(text, cfg=None, voice=None, language=None, speed=None, engine=None, save_file=False):
    """Synthesize speech into WAV bytes + base64 data URL using XTTS v2 or Windows SAPI5 fallback."""
    cfg = config_mod.load() if cfg is None else cfg
    cleaned = clean_speech_text(text)
    if not cleaned:
        return {"ok": False, "error": "Пустой текст для озвучки"}

    eng = str(engine or cfg.get("voice_engine") or "auto").lower()
    voice_id = voice or cfg.get("voice_preset") or "ru_female_warm"
    lang = language or cfg.get("voice_language") or "ru"
    spd = float(speed if speed is not None else (cfg.get("voice_speed") or 1.0))
    xtts_url = cfg.get("voice_xtts_url") or "http://127.0.0.1:8020"

    res = None
    if eng in ("xtts", "xtts_v2", "auto"):
        speaker_wav = _resolve_speaker_wav(voice_id, cfg)
        probe = check_xtts_server(xtts_url, timeout=1.2)
        if probe["alive"] or eng in ("xtts", "xtts_v2"):
            res = _synthesize_xtts_http(cleaned, xtts_url, speaker_wav, language=lang, speed=spd)
            if not res.get("ok") and eng in ("xtts", "xtts_v2") and os.name == "nt":
                # Fallback to SAPI5 with a note if XTTS server is down
                sapi = _synthesize_sapi_wav(cleaned, voice_id=voice_id, speed=spd)
                if sapi.get("ok"):
                    sapi["note"] = "XTTS v2 (%s) недоступен, использован резервный движок Windows SAPI5" % xtts_url
                    res = sapi

    if not res or not res.get("ok"):
        res = _synthesize_sapi_wav(cleaned, voice_id=voice_id, speed=spd)

    if not res.get("ok"):
        return res

    audio_bytes = res["audio"]
    b64 = base64.b64encode(audio_bytes).decode("ascii")
    out = {
        "ok": True,
        "engine_used": res.get("engine_used", "unknown"),
        "text": cleaned,
        "voice": voice_id,
        "language": lang,
        "bytes": len(audio_bytes),
        "audio_b64": "data:audio/wav;base64," + b64,
    }
    if res.get("note"):
        out["note"] = res["note"]
    if save_file:
        fname = "tts_%s.wav" % time.strftime("%Y%m%d_%H%M%S")
        fpath = os.path.join(out_dir(), fname)
        with open(fpath, "wb") as fh:
            fh.write(audio_bytes)
        out["file"] = fpath
    return out

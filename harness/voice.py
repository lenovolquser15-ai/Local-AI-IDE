"""Voice Engine 2.0 for LocalHarness:
- Studio-grade Neural TTS via Microsoft Edge Neural (`edge_tts`, 24kHz MP3, RU/EN/Multilingual)
- XTTS v2 zero-shot voice cloning & local HTTP synthesis (`http://127.0.0.1:8020`)
- Offline Windows SAPI5 WAV fallback (`System.Speech.Synthesis`)
- Sentence chunking for low-latency streaming voice calls
"""
import asyncio
import base64
import importlib.util
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.request

from . import config as config_mod
from . import dnsfix  # noqa: F401 — installs getaddrinfo fallback for edge-tts aiohttp

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_LOCK = threading.RLock()

BUILTIN_PRESETS = [
    {
        "id": "ru_female_warm",
        "name": "Светлана Neural (Тёплый женский · RU)",
        "edge_voice": "ru-RU-SvetlanaNeural",
        "lang": "ru",
        "gender": "female",
        "builtin": True,
        "neural": True,
        "desc": "Естественный студийный женский голос (Edge Neural / XTTS v2) для звонков и чата",
    },
    {
        "id": "ru_male_calm",
        "name": "Дмитрий Neural (Спокойный баритон · RU)",
        "edge_voice": "ru-RU-DmitryNeural",
        "lang": "ru",
        "gender": "male",
        "builtin": True,
        "neural": True,
        "desc": "Уверенный мужской баритон для технической озвучки и ассистента",
    },
    {
        "id": "ru_dariya",
        "name": "Дарья Neural (Живой женский · RU)",
        "edge_voice": "ru-RU-DariyaNeural",
        "lang": "ru",
        "gender": "female",
        "builtin": True,
        "neural": True,
        "desc": "Чёткий и динамичный женский голос для быстрых ответов",
    },
    {
        "id": "en_nova",
        "name": "Ava Multilingual Neural (RU / EN / DE / ZH)",
        "edge_voice": "en-US-AvaMultilingualNeural",
        "lang": "en",
        "gender": "female",
        "builtin": True,
        "neural": True,
        "desc": "Мультиязычный голос нового поколения — свободно читает русский и английский в одной фразе",
    },
    {
        "id": "en_andrew",
        "name": "Andrew Multilingual Neural (Мужской · Multi)",
        "edge_voice": "en-US-AndrewMultilingualNeural",
        "lang": "en",
        "gender": "male",
        "builtin": True,
        "neural": True,
        "desc": "Глубокий мультиязычный мужской голос (RU / EN / EU)",
    },
    {
        "id": "en_aria",
        "name": "Aria Neural (Expressive · EN)",
        "edge_voice": "en-US-AriaNeural",
        "lang": "en",
        "gender": "female",
        "builtin": True,
        "neural": True,
        "desc": "Выразительный англоязычный нейронный голос",
    },
]

_PRESET_BY_ID = {p["id"]: p for p in BUILTIN_PRESETS}
_EDGE_AVAIL = None
_EDGE_DOWN_UNTIL = 0.0


def edge_tts_available():
    global _EDGE_AVAIL
    if _EDGE_AVAIL is None:
        _EDGE_AVAIL = importlib.util.find_spec("edge_tts") is not None
    return _EDGE_AVAIL


def voices_dir():
    d = os.path.join(config_mod.data_dir(), "voices")
    os.makedirs(d, exist_ok=True)
    return d


def out_dir():
    d = os.path.join(config_mod.data_dir(), "voice_out")
    os.makedirs(d, exist_ok=True)
    return d


def clean_speech_text(text, max_chars=2200):
    """Strip markdown, code blocks, URLs, and noisy punctuation for natural TTS reading."""
    s = str(text or "")
    s = re.sub(r"<think>[\s\S]*?</think>", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"```[\s\S]*?```", " [блок кода] ", s)
    s = re.sub(r"`([^`]{1,80})`", r"\1", s)
    s = re.sub(r"https?://\S+", " ссылка ", s)
    s = re.sub(r"[*_~]+", "", s)
    s = re.sub(r"[#>|]+", " ", s)
    s = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_chars]


def split_sentences(text, max_chunk=220):
    """Split response text into natural sentence chunks for low-latency streaming voice calls."""
    cleaned = clean_speech_text(text, max_chars=3000)
    if not cleaned:
        return []
    raw_parts = re.split(r"(?<=[.!?…;])\s+", cleaned)
    chunks = []
    buf = ""
    for part in raw_parts:
        part = part.strip()
        if not part:
            continue
        if not buf:
            buf = part
        elif len(buf) + 1 + len(part) <= max_chunk:
            buf = buf + " " + part
        else:
            chunks.append(buf)
            buf = part
    if buf:
        chunks.append(buf)
    return chunks


def list_presets(cfg=None):
    """Return built-in neural voice presets plus user-uploaded .wav samples for XTTS v2 cloning."""
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
                "neural": True,
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


def check_xtts_server(url, timeout=1.5):
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
    probe = check_xtts_server(xtts_url, timeout=1.2)
    presets = list_presets(cfg)
    return {
        "ok": True,
        "engine": cfg.get("voice_engine", "auto"),
        "xtts_url": xtts_url,
        "xtts_alive": probe["alive"],
        "edge_available": edge_tts_available(),
        "voice_preset": cfg.get("voice_preset", "ru_female_warm"),
        "voice_language": cfg.get("voice_language", "ru"),
        "voice_speed": float(cfg.get("voice_speed") or 1.0),
        "voice_pitch": int(cfg.get("voice_pitch") or 0),
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
    vdir = voices_dir()
    try:
        wavs = [os.path.join(vdir, f) for f in sorted(os.listdir(vdir)) if f.lower().endswith(".wav")]
        if wavs:
            return wavs[0]
    except OSError:
        pass
    return vid or "default"


def _resolve_edge_voice(voice_id, language="ru"):
    vid = str(voice_id or "").strip()
    if vid in _PRESET_BY_ID and _PRESET_BY_ID[vid].get("edge_voice"):
        return _PRESET_BY_ID[vid]["edge_voice"]
    if "Neural" in vid and "-" in vid:
        return vid
    lang = str(language or "ru").lower()
    is_male = "male" in vid.lower() or "dmitry" in vid.lower() or "andrew" in vid.lower()
    if lang.startswith("en"):
        return "en-US-AndrewMultilingualNeural" if is_male else "en-US-AvaMultilingualNeural"
    return "ru-RU-DmitryNeural" if is_male else "ru-RU-SvetlanaNeural"


def _synthesize_edge_neural(text, voice_id="ru_female_warm", language="ru", speed=1.0, pitch=0, timeout=4.5):
    """Studio-quality Neural TTS via Microsoft Edge Neural (`edge_tts`) with fast circuit-breaker."""
    global _EDGE_DOWN_UNTIL
    if not edge_tts_available():
        return {"ok": False, "error": "Пакет edge-tts не установлен"}
    if time.time() < _EDGE_DOWN_UNTIL:
        return {"ok": False, "error": "Edge Neural временно недоступен в текущей сети (активен офлайн-движок)"}
    import edge_tts

    edge_voice = _resolve_edge_voice(voice_id, language=language)
    pct = int(max(-50, min(100, round((float(speed or 1.0) - 1.0) * 100))))
    rate_str = ("%+d%%" % pct)
    hz = int(max(-30, min(30, int(pitch or 0))))
    pitch_str = ("%+dHz" % hz)

    async def _collect():
        comm = edge_tts.Communicate(text, voice=edge_voice, rate=rate_str, pitch=pitch_str)
        chunks = []
        async for item in comm.stream():
            if item.get("type") == "audio" and item.get("data"):
                chunks.append(item["data"])
        return b"".join(chunks)

    try:
        loop = asyncio.new_event_loop()
        try:
            audio = loop.run_until_complete(asyncio.wait_for(_collect(), timeout=timeout))
        finally:
            loop.close()
        if audio and len(audio) > 120:
            _EDGE_DOWN_UNTIL = 0.0
            return {
                "ok": True,
                "audio": audio,
                "mime": "audio/mpeg",
                "ext": "mp3",
                "engine_used": "edge_neural",
                "neural_voice": edge_voice,
            }
        return {"ok": False, "error": "Edge Neural TTS вернул пустой аудиопоток"}
    except Exception as exc:
        _EDGE_DOWN_UNTIL = time.time() + 180.0
        return {"ok": False, "error": "Edge Neural TTS недоступен (%s)" % (type(exc).__name__ if not str(exc) else exc)}


def _synthesize_xtts_http(text, xtts_url, speaker_wav, language="ru", speed=1.0, timeout=45.0):
    """Call local XTTS v2 server (xtts-api-server / Coqui server / OpenAI-compatible /v1/audio/speech)."""
    base = str(xtts_url or "http://127.0.0.1:8020").rstrip("/")
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
                    return {"ok": True, "audio": audio, "mime": "audio/wav", "ext": "wav", "engine_used": "xtts_v2"}
        except Exception as exc:
            last_err = str(exc)
    return {"ok": False, "error": last_err or "XTTS v2 сервер не ответил"}


def _synthesize_sapi_wav(text, voice_id="ru_female_warm", speed=1.0, pitch=0):
    """Offline Windows OneCore WinRT (Irina/Pavel + SSML prosody) & SAPI5 WAV synthesis."""
    if os.name != "nt":
        return {"ok": False, "error": "Windows TTS доступен только на Windows"}
    is_male = any(k in str(voice_id).lower() for k in ("male", "dmitry", "andrew", "pavel"))
    gender_str = "Male" if is_male else "Female"
    spd = max(0.5, min(2.0, float(speed or 1.0)))
    hz = int(max(-30, min(30, int(pitch or 0))))
    pitch_ssml = ("%+dHz" % hz) if hz else "default"
    tmp_wav = os.path.join(tempfile.gettempdir(), "lh_tts_%d_%d.wav" % (os.getpid(), int(time.time() * 1000)))
    safe_path = tmp_wav.replace("'", "''")

    # Escape XML chars for SSML
    xml_text = (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "''")
        .replace("\r", " ")
        .replace("\n", " ")
    )

    ps_winrt = (
        "try { "
        "[Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media.SpeechSynthesis, ContentType = WindowsRuntime] | Out-Null; "
        "[Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null; "
        "$synth = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer; "
        "$targetGender = [Windows.Media.SpeechSynthesis.VoiceGender]::%s; "
        "$v = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices | Where-Object { $_.Gender -eq $targetGender } | Select-Object -First 1; "
        "if ($v) { $synth.Voice = $v }; "
        "$lang = if ($synth.Voice) { $synth.Voice.Language } else { 'ru-RU' }; "
        "$ssml = \"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='$lang'><prosody rate='%.2f' pitch='%s'>%s</prosody></speak>\"; "
        "$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]; "
        "$stream = $asTask.MakeGenericMethod([Windows.Media.SpeechSynthesis.SpeechSynthesisStream]).Invoke($null, @($synth.SynthesizeSsmlToStreamAsync($ssml))).GetAwaiter().GetResult(); "
        "$size = [uint32]$stream.Size; "
        "$reader = New-Object Windows.Storage.Streams.DataReader($stream.GetInputStreamAt(0)); "
        "$asTaskLoad = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'DataReaderLoadOperation' })[0]; "
        "if ($asTaskLoad) { $asTaskLoad.Invoke($null, @($reader.LoadAsync($size))).GetAwaiter().GetResult() | Out-Null } "
        "else { while ($reader.UnconsumedBufferLength -lt $size) { $op = $reader.LoadAsync($size); while ($op.Status -eq 0) { Start-Sleep -Milliseconds 10 }; break } }; "
        "$bytes = New-Object byte[] $size; "
        "$reader.ReadBytes($bytes); "
        "[System.IO.File]::WriteAllBytes('%s', $bytes); "
        "$reader.Dispose(); $stream.Dispose(); $synth.Dispose(); exit 0 "
        "} catch { exit 1 }"
    ) % (gender_str, spd, pitch_ssml, xml_text, safe_path)

    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps_winrt],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=18,
            creationflags=NO_WINDOW,
        )
        if os.path.isfile(tmp_wav) and os.path.getsize(tmp_wav) > 100:
            with open(tmp_wav, "rb") as fh:
                audio = fh.read()
            try:
                os.remove(tmp_wav)
            except OSError:
                pass
            return {"ok": True, "audio": audio, "mime": "audio/wav", "ext": "wav", "engine_used": "windows_onecore"}
    except Exception:
        pass

    # Classic System.Speech SAPI5 fallback
    rate = int(max(-5, min(5, round((spd - 1.0) * 6))))
    safe_text = str(text).replace("'", "''").replace("\r", " ").replace("\n", " ")
    ps_script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Rate = %d; "
        "try { $s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::%s) } catch {}; "
        "$s.SetOutputToWaveFile('%s'); "
        "$s.Speak('%s'); "
        "$s.Dispose()"
    ) % (rate, gender_str, safe_path, safe_text)
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
            return {"ok": True, "audio": audio, "mime": "audio/wav", "ext": "wav", "engine_used": "windows_sapi5"}
    except Exception as exc:
        return {"ok": False, "error": "SAPI5 ошибка: %s" % exc}
    finally:
        if os.path.exists(tmp_wav):
            try:
                os.remove(tmp_wav)
            except OSError:
                pass
    return {"ok": False, "error": "Не удалось синтезировать WAV через Windows TTS"}


def synthesize(text, cfg=None, voice=None, language=None, speed=None, pitch=None, engine=None, save_file=False):
    """Synthesize speech into audio bytes + base64 data URL using XTTS v2, Edge Neural TTS, or Windows SAPI5."""
    cfg = config_mod.load() if cfg is None else cfg
    cleaned = clean_speech_text(text)
    if not cleaned:
        return {"ok": False, "error": "Пустой текст для озвучки"}

    eng = str(engine or cfg.get("voice_engine") or "auto").lower()
    voice_id = voice or cfg.get("voice_preset") or "ru_female_warm"
    lang = language or cfg.get("voice_language") or "ru"
    spd = float(speed if speed is not None else (cfg.get("voice_speed") or 1.0))
    ptc = int(pitch if pitch is not None else (cfg.get("voice_pitch") or 0))
    xtts_url = cfg.get("voice_xtts_url") or "http://127.0.0.1:8020"

    res = None

    # 1. Explicit Edge Neural TTS
    if eng in ("edge", "edge_neural", "neural"):
        res = _synthesize_edge_neural(cleaned, voice_id=voice_id, language=lang, speed=spd, pitch=ptc)
        if not res.get("ok") and os.name == "nt":
            sapi = _synthesize_sapi_wav(cleaned, voice_id=voice_id, speed=spd, pitch=ptc)
            if sapi.get("ok"):
                sapi["note"] = "Edge Neural TTS недоступен, использован офлайн Windows OneCore/SAPI5"
                res = sapi

    # 2. XTTS v2 or Auto (XTTS v2 -> Edge Neural TTS -> Windows OneCore/SAPI5)
    elif eng in ("xtts", "xtts_v2", "auto"):
        speaker_wav = _resolve_speaker_wav(voice_id, cfg)
        probe = check_xtts_server(xtts_url, timeout=1.0)
        if probe["alive"] or eng in ("xtts", "xtts_v2"):
            res = _synthesize_xtts_http(cleaned, xtts_url, speaker_wav, language=lang, speed=spd)
        if (not res or not res.get("ok")) and edge_tts_available():
            edge_res = _synthesize_edge_neural(cleaned, voice_id=voice_id, language=lang, speed=spd, pitch=ptc)
            if edge_res.get("ok"):
                if eng in ("xtts", "xtts_v2"):
                    edge_res["note"] = "XTTS v2 (%s) недоступен — использован студийный Edge Neural TTS" % xtts_url
                res = edge_res
        if (not res or not res.get("ok")) and os.name == "nt":
            sapi = _synthesize_sapi_wav(cleaned, voice_id=voice_id, speed=spd, pitch=ptc)
            if sapi.get("ok"):
                if eng in ("xtts", "xtts_v2"):
                    sapi["note"] = "XTTS v2 (%s) недоступен, использован резервный движок Windows OneCore/SAPI5" % xtts_url
                res = sapi

    # 3. Explicit offline OneCore/SAPI5
    else:
        res = _synthesize_sapi_wav(cleaned, voice_id=voice_id, speed=spd, pitch=ptc)

    if not res or not res.get("ok"):
        return res or {"ok": False, "error": "Не удалось озвучить текст"}

    audio_bytes = res["audio"]
    mime = res.get("mime") or ("audio/mpeg" if res.get("engine_used") == "edge_neural" else "audio/wav")
    ext = res.get("ext") or ("mp3" if mime == "audio/mpeg" else "wav")
    b64 = base64.b64encode(audio_bytes).decode("ascii")
    out = {
        "ok": True,
        "engine_used": res.get("engine_used", "unknown"),
        "mime": mime,
        "text": cleaned,
        "voice": voice_id,
        "language": lang,
        "bytes": len(audio_bytes),
        "audio_b64": "data:%s;base64,%s" % (mime, b64),
    }
    if res.get("neural_voice"):
        out["neural_voice"] = res["neural_voice"]
    if res.get("note"):
        out["note"] = res["note"]
    if save_file:
        fname = "tts_%s_%d.%s" % (time.strftime("%Y%m%d_%H%M%S"), int(time.time() * 1000) % 1000, ext)
        fpath = os.path.join(out_dir(), fname)
        with open(fpath, "wb") as fh:
            fh.write(audio_bytes)
        out["file"] = fpath
    return out

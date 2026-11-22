"""Author Plugin: XTTS v2 Voice Studio & Zero-Shot Voice Cloning for LocalHarness.

Provides AI agent tools to synthesize speech into WAV files, register custom reference
WAV samples for XTTS v2 zero-shot voice cloning, and inspect available voice presets.
"""
import base64
import os

from harness import voice as voice_mod

NAME = "xtts_voice_studio"
AUTHOR = "LocalHarness Core Team"
VERSION = "1.0"
DESC = "Авторский плагин голосового синтеза и клонирования голоса XTTS v2 (Coqui TTS + Windows SAPI5 fallback)."


def _tool_speak(cfg, args):
    text = str(args.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "Укажите text для озвучки"}
    voice_id = args.get("voice") or cfg.get("voice_preset") or "ru_female_warm"
    lang = args.get("language") or cfg.get("voice_language") or "ru"
    speed = float(args.get("speed") or cfg.get("voice_speed") or 1.0)
    res = voice_mod.synthesize(
        text,
        cfg=cfg,
        voice=voice_id,
        language=lang,
        speed=speed,
        save_file=True,
    )
    if not res.get("ok"):
        return res
    return {
        "ok": True,
        "engine_used": res.get("engine_used"),
        "file": res.get("file"),
        "bytes": res.get("bytes"),
        "voice": voice_id,
        "language": lang,
        "note": res.get("note") or "Аудиофайл успешно синтезирован и сохранён.",
    }


def _tool_clone(cfg, args):
    wav_path = str(args.get("wav_path") or "").strip().strip('"')
    name = str(args.get("name") or "").strip()
    if not wav_path or not os.path.isfile(wav_path):
        return {"ok": False, "error": "Файл образца голоса не найден: %s" % wav_path}
    if not name:
        name = os.path.splitext(os.path.basename(wav_path))[0]
    try:
        with open(wav_path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
    except Exception as exc:
        return {"ok": False, "error": "Не удалось прочитать WAV: %s" % exc}
    return voice_mod.save_sample(name, b64)


def _tool_presets(cfg, args):
    st = voice_mod.status(cfg)
    return {
        "ok": True,
        "engine": st["engine"],
        "xtts_url": st["xtts_url"],
        "xtts_alive": st["xtts_alive"],
        "presets": st["presets"],
    }


TOOLS = [
    {
        "name": "voice_speak",
        "desc": "Синтезировать речь (XTTS v2 / SAPI5) из текста и сохранить в WAV-файл",
        "params": {
            "text": ("string", "текст для озвучки"),
            "voice": ("string?", "ID пресета или sample:<file.wav>"),
            "language": ("string?", "код языка: ru, en, de, fr, es, zh"),
            "speed": ("number?", "скорость речи (0.5 - 2.0, по умолчанию 1.0)"),
        },
        "required": ["text"],
        "fn": _tool_speak,
    },
    {
        "name": "voice_clone_sample",
        "desc": "Добавить локальный WAV-файл (6-30 сек чистой речи) как клон голоса для XTTS v2",
        "params": {
            "wav_path": ("string", "путь к референсному .wav файлу"),
            "name": ("string?", "название нового голосового пресета"),
        },
        "required": ["wav_path"],
        "fn": _tool_clone,
    },
    {
        "name": "voice_list_presets",
        "desc": "Показать список доступных голосов, клонов XTTS v2 и статус голосового движка",
        "params": {},
        "required": [],
        "fn": _tool_presets,
    },
]

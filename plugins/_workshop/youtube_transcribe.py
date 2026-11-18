"""YouTube Video Transcribe and Subtitles Plugin for LocalHarness.

Fetches transcript and spoken text from YouTube videos without browser emulation.
"""
import re
import urllib.parse
import xml.etree.ElementTree as ET
import requests

NAME = "youtube_transcribe"


def _extract_video_id(url_or_id):
    raw = (url_or_id or "").strip()
    if re.match(r"^[A-Za-z0-9_-]{11}$", raw):
        return raw
    m = re.search(r"(?:v=|\/)([A-Za-z0-9_-]{11})(?:[&?]|$)", raw)
    return m.group(1) if m else raw


def _get_transcript(cfg, args):
    video = _extract_video_id(args.get("video_id") or args.get("url"))
    if not video:
        return {"ok": False, "error": "укажите ссылку на YouTube или ID видео"}

    # Fetch watch page to find timedtext player caption URL
    watch_url = f"https://www.youtube.com/watch?v={video}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = requests.get(watch_url, headers=headers, timeout=15)
        html = r.text
        m = re.search(r'("captionTracks":\s*\[.*?\])', html)
        if not m:
            return {"ok": False, "error": "Субтитры для данного видео не найдены (или видео защищено/приватно)"}

        raw_match = "{" + m.group(1) + "}"
        import json
        data = json.loads(raw_match)
        tracks = data.get("captionTracks", [])
        if not tracks:
            return {"ok": False, "error": "Список субтитров пуст"}

        wanted_lang = (args.get("lang") or "ru").lower()
        chosen = next((t for t in tracks if t.get("languageCode", "").lower().startswith(wanted_lang)), tracks[0])
        base_url = chosen.get("baseUrl")
        if not base_url:
            return {"ok": False, "error": "Не удалось извлечь адрес дорожки субтитров"}

        sub_resp = requests.get(base_url, headers=headers, timeout=12)
        root = ET.fromstring(sub_resp.text)
        lines = []
        for elem in root.findall(".//text"):
            txt = (elem.text or "").strip()
            if txt:
                lines.append(txt)

        full_text = " ".join(lines)
        return {
            "ok": True,
            "video_id": video,
            "language": chosen.get("languageCode"),
            "lines_count": len(lines),
            "transcript": full_text[:40000],
        }
    except Exception as exc:
        return {"ok": False, "error": f"Ошибка извлечения субтитров: {exc}"}


TOOLS = [
    {
        "name": "youtube_transcript",
        "desc": "Извлечь полный текст и субтитры YouTube-видео для анализа и суммаризации",
        "params": {
            "url": ("string", "ссылка на YouTube видео (или ID)"),
            "lang": ("string?", "желаемый язык субтитров (ru, en)"),
        },
        "required": ["url"],
        "fn": _get_transcript,
    },
]

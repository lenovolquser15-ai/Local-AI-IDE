"""Vision support: image encoding for OpenAI-compatible chat, multimodal content helpers.

Images travel as data: URLs inside content parts. Text-only models get the text
with a note instead, and context folding counts an image as a fixed budget so a
base64 blob never blows up the char budget or gets cut in half.
"""
import base64
import os
import re

IMAGE_COST = 1200
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_DATA_URL = 15_000_000
ALLOWED_MIME = ("image/png", "image/jpeg", "image/webp", "image/gif")
_MIME_BY_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif"}

VISION_HINTS = ("llava", "qwen2-vl", "qwen2.5-vl", "qwen3-vl", "qwen-vl", "gemma-3", "gemma3",
                "ministral-3", "ministral3", "pixtral", "moondream", "internvl", "minicpm-v",
                "phi-3-vision", "phi-4-multimodal", "cogvlm", "yi-vl", "glm-4v", "molmo",
                "smolvlm", "aria", "vision", "multimodal")


def supports_vision(cfg, model=None):
    """auto: флаг vision из lms ls, затем эвристика по имени; vision=true/false — явно."""
    flag = cfg.get("vision", "auto")
    if flag is True:
        return True
    if flag is False:
        return False
    key = str(model or cfg.get("model") or "").lower()
    if not key:
        return False
    try:
        from . import lms
        items, _err = lms.disk_models(cfg, timeout=8)
        for item in items:
            if not item.get("vision"):
                continue
            mk = lms.model_key(item).lower()
            name = str(item.get("displayName") or "").lower()
            if mk and (mk == key or key in mk or mk in key or (name and name in key)):
                return True
    except Exception:
        pass
    return any(hint in key for hint in VISION_HINTS)


def encode_path(path, max_dim=1440, quality=83):
    """Локальный файл -> data URL. Скриншоты жмутся в JPEG, чтобы не раздувать контекст."""
    path = os.path.expanduser(str(path))
    if not os.path.isfile(path):
        return None
    size = os.path.getsize(path)
    if size > MAX_UPLOAD_BYTES:
        return None
    ext = os.path.splitext(path)[1].lower()
    mime = _MIME_BY_EXT.get(ext)
    if not mime:
        return None
    try:
        from PIL import Image
        with Image.open(path) as im:
            if im.format == "GIF":
                with open(path, "rb") as fh:
                    return "data:%s;base64,%s" % (mime, base64.b64encode(fh.read()).decode("ascii"))
            im = im.convert("RGB")
            im.thumbnail((max_dim, max_dim))
            import io
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality)
            return "data:image/jpeg;base64,%s" % base64.b64encode(buf.getvalue()).decode("ascii")
    except ImportError:
        with open(path, "rb") as fh:
            return "data:%s;base64,%s" % (mime, base64.b64encode(fh.read()).decode("ascii"))
    except Exception:
        return None


def normalize_image(value):
    """Принимает data URL из UI, локальный путь или http(s); возвращает data URL либо None."""
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("data:"):
        header, _, payload = value.partition(",")
        mime = header[5:].split(";")[0].lower()
        if mime not in ALLOWED_MIME or not payload or len(value) > MAX_DATA_URL:
            return None
        return value
    if value.lower().startswith(("http://", "https://")):
        try:
            from . import netio
            res = netio.request("GET", value, timeout=60.0, max_bytes=MAX_UPLOAD_BYTES)
            if res.get("status", 0) >= 400:
                return None
            body = res.get("body") or b""
            mime = (res.get("headers", {}) or {}).get("content-type", "").split(";")[0].strip().lower()
            if mime not in ALLOWED_MIME:
                ext = os.path.splitext(value.split("?")[0])[1].lower()
                mime = _MIME_BY_EXT.get(ext, "image/png")
            return "data:%s;base64,%s" % (mime, base64.b64encode(body).decode("ascii"))
        except Exception:
            return None
    return encode_path(value)


def image_part(data_url):
    return {"type": "image_url", "image_url": {"url": data_url}}


def content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        bits = [p.get("text") or "" for p in content
                if isinstance(p, dict) and p.get("type") == "text"]
        return "\n".join(b for b in bits if b)
    return ""


def content_images(content):
    if isinstance(content, list):
        return [p for p in content if isinstance(p, dict) and p.get("type") == "image_url"]
    return []


def estimate_content(content):
    if isinstance(content, str):
        return len(content)
    return len(content_text(content)) + IMAGE_COST * len(content_images(content))


def text_only(content):
    """Для модели без vision: текст + честная пометка про картинки."""
    if isinstance(content, str):
        return content
    text = content_text(content)
    count = len(content_images(content))
    if count:
        text += ("\n" if text else "") + "[изображений: %d — модель без vision, картинки не переданы]" % count
    return text


def make_user_content(text, images):
    images = [i for i in (images or []) if i]
    if not images:
        return text
    parts = [{"type": "text", "text": text or "Посмотри на изображение."}]
    parts.extend(image_part(i) for i in images)
    return parts


_DATA_URL_RE = re.compile(r"^data:image/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+$")


def is_data_url(value):
    return bool(isinstance(value, str) and _DATA_URL_RE.match(value))
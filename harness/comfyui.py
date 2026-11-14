"""ComfyUI and SD WebUI client for local image generation in LocalHarness."""
import base64
import json
import os
import time
import urllib.request
import urllib.error
import uuid

from .config import DATA_DIR


def _post_json(url, payload, timeout=120):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url, timeout=10):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def status(cfg):
    url = (cfg.get("comfyui_url") or "http://127.0.0.1:8188").rstrip("/")
    try:
        data = _get_json(url + "/system_stats", timeout=3)
        return {"ok": True, "type": "comfyui", "url": url, "stats": data}
    except Exception:
        pass
    # check SD WebUI
    try:
        data = _get_json(url + "/sdapi/v1/options", timeout=3)
        return {"ok": True, "type": "sdwebui", "url": url}
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}


def generate_image(cfg, prompt, negative_prompt="", width=512, height=512, steps=20, seed=None):
    if not cfg.get("image_gen_enabled", False):
        return {"ok": False, "error": "Генерация изображений отключена в настройках (image_gen_enabled: false)."}

    url = (cfg.get("comfyui_url") or "http://127.0.0.1:8188").rstrip("/")
    out_dir = os.path.join(DATA_DIR, "shots")
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "gen_%s_%s.png" % (time.strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:4]))

    # 1. Try SD WebUI / Forge API first if available on this url or :7860
    sd_urls = [url]
    if ":8188" in url:
        sd_urls.append(url.replace(":8188", ":7860"))
    for s_url in sd_urls:
        try:
            body = {
                "prompt": prompt,
                "negative_prompt": negative_prompt or "",
                "width": int(width or 512),
                "height": int(height or 512),
                "steps": int(steps or 20),
                "seed": int(seed if seed is not None else -1),
            }
            res = _post_json(s_url + "/sdapi/v1/txt2img", body, timeout=180)
            images = res.get("images", [])
            if images:
                raw = base64.b64decode(images[0])
                with open(out_file, "wb") as fh:
                    fh.write(raw)
                return {"ok": True, "path": out_file, "image": out_file,
                        "note": "изображение успешно сгенерировано через SD WebUI (%s)" % s_url}
        except Exception:
            pass

    # 2. Try ComfyUI API
    try:
        # Standard default ComfyUI txt2img workflow prompt
        client_id = uuid.uuid4().hex
        workflow = {
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "cfg": 7, "denoise": 1, "latent_image": ["5", 0],
                    "model": ["4", 0], "negative": ["7", 0], "positive": ["6", 0],
                    "sampler_name": "euler", "scheduler": "normal",
                    "seed": int(seed if seed is not None else int(time.time() * 1000) % 1000000000),
                    "steps": int(steps or 20),
                }
            },
            "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ""}},
            "5": {"class_type": "EmptyLatentImage", "inputs": {"batch_size": 1, "height": int(height or 512), "width": int(width or 512)}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["4", 1], "text": str(prompt)}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["4", 1], "text": str(negative_prompt or "")}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "LocalHarness", "images": ["8", 0]}},
        }
        # First query /object_info to get an installed checkpoint name if empty
        try:
            obj_info = _get_json(url + "/object_info/CheckpointLoaderSimple")
            ckpts = obj_info.get("CheckpointLoaderSimple", {}).get("input", {}).get("required", {}).get("ckpt_name", [[]])[0]
            if ckpts:
                workflow["4"]["inputs"]["ckpt_name"] = ckpts[0]
        except Exception:
            pass

        p_res = _post_json(url + "/prompt", {"prompt": workflow, "client_id": client_id})
        prompt_id = p_res.get("prompt_id")
        if not prompt_id:
            return {"ok": False, "error": "ComfyUI не вернул prompt_id: %s" % p_res}

        # Poll history for finished image
        deadline = time.time() + 180
        while time.time() < deadline:
            time.sleep(1.5)
            hist = _get_json(url + "/history/" + prompt_id)
            if prompt_id in hist:
                outputs = hist[prompt_id].get("outputs", {})
                for node_id, node_out in outputs.items():
                    imgs = node_out.get("images", [])
                    if imgs:
                        fname = imgs[0].get("filename")
                        subfolder = imgs[0].get("subfolder", "")
                        itype = imgs[0].get("type", "output")
                        img_url = "%s/view?filename=%s&subfolder=%s&type=%s" % (url, fname, subfolder, itype)
                        req = urllib.request.Request(img_url)
                        with urllib.request.urlopen(req, timeout=30) as resp:
                            with open(out_file, "wb") as fh:
                                fh.write(resp.read())
                        return {"ok": True, "path": out_file, "image": out_file,
                                "note": "изображение успешно создано через ComfyUI"}
                break
    except Exception as exc:
        return {"ok": False, "error": (
            "ComfyUI/SD WebUI недоступен по адресу %s (%s). "
            "Убедитесь, что ComfyUI запущен, или укажите правильный порт в Настройках."
            % (url, exc))}

    return {"ok": False, "error": "таймаут ожидания генерации от ComfyUI"}

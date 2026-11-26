import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from harness import dnsfix  # noqa: E402, F401 — enables getaddrinfo fallback for GitHub API
import requests  # noqa: E402


def get_token():
    p = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        text=True,
        capture_output=True,
        check=True,
    )
    for line in p.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("Could not find token in git credential fill")


def main():
    token = get_token()
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "LocalHarness-Release-Script",
    }

    tag = "v1.0.8-beta.1"
    name = "LocalHarness v1.0.8-beta.1 (Pre-Release)"
    body = (
        "**Telegram-канал:** [t.me/RemingOstai](https://t.me/RemingOstai)\n\n"
        "## 🚀 Крупное предрелизное обновление LocalHarness v1.0.8-beta.1\n\n"
        "### 🧬 1. LoRA & Unsloth Fine-Tuning Studio 3.0\n"
        "- **Двойной движок обучения:** скоростной **Unsloth 4-bit QLoRA** (SFT, GRPO Reasoning RL, DPO, Vision VLM) + универсальный **PEFT (`peft_universal`)** с поддержкой **NVIDIA CUDA, AMD ROCm и многоядерного CPU (MKLDNN/AVX2)**, автоматическим откатом на CPU при нехватке VRAM и маскированием `-100` только на ответах ассистента.\n"
        "- **0-VRAM Инспектор слоёв (`🔍 Слои модели`):** мгновенный анализ заголовков `safetensors` / архитектуры без загрузки весов в видеопамять и ручной выбор целевых проекций `target_modules` (`q_proj`, `k_proj`, `v_proj`, `o_proj`, `gate_proj`, `up_proj`, `down_proj`).\n"
        "- **HuggingFace Hub + ИИ-синтетика + Очистка датасетов:** поиск и многопоточное скачивание базовых моделей прямо в `data/models`, сбор датасетов из чатов, ИИ-генератор примеров, импорт CSV/JSONL и **1-клик дедупликатор/валидатор JSONL** со статистикой токенов.\n"
        "- **Автономный квантизатор и бенчмарк GGUF (`llama.cpp`):** сжатие любых `.gguf` моделей в **15 форматов** (`Q4_K_M`, `Q5_K_M`, `Q6_K`, `Q8_0`, `IQ4_XS` и др.) через `llama-quantize.exe`, замер реального `tok/s` через `llama-bench.exe`, живая SVG-кривая Loss, скорость `шаг/с`, `ETA` и термозащита GPU при обучении.\n\n"
        "### 📞 2. Голосовой движок 2.0 и Потоковый Созвон с инструментами\n"
        "- **Студийные нейронные голоса (`Microsoft Edge Neural TTS` 24 кГц)** (`Светлана`, `Дмитрий`, `Дарья`, мультиязычные `Ava` и `Andrew`, `Aria`) + локальное zero-shot клонирование голоса **Coqui XTTS v2** + офлайн-резерв **Windows OneCore (`Irina`/`Pavel`) / SAPI5**.\n"
        "- **Запись WAV-клона прямо в браузере:** кнопка `🔴 Запись WAV` записывает 7 секунд чистой речи с микрофона в 16-bit PCM `.wav` без сторонних редакторов + настройка скорости (`0.6×–1.8×`) и высоты тона (`Hz`).\n"
        "- **Голосовой созвон 2.1 (`📞 Созвон`):** потоковая озвучка по предложениям (`⚡ Поток`) прямо во время генерации ответа, живые субтитры распознавания речи в реальном времени, Push-to-Talk по `Пробелу`, WebAudio-визуализация и полный доступ ко всем инструментам агента (поиск, код, скриншоты, RAG, MCP, режим цели).\n\n"
        "### 📁 3. Встроенный IDE Редактор + Мобильный доступ по Wi-Fi (`/files`)\n"
        "- Новая вкладка **«📁 Файлы»** с деревом рабочей папки, быстрым поиском, созданием файлов/папок, сохранением по `Ctrl+S`, защитой от конфликтов ревизий (`409`), отправкой файла агенту в чат и индексацией в **RAG**.\n"
        "- Отдельный мобильный веб-редактор `/files.html` с 10-значным кодом сопряжения по домашнему Wi-Fi.\n\n"
        "### 🧩 4. Плагины 2.0, Локальная база знаний RAG и MCP\n"
        "- **Горячее редактирование кода плагинов** с AST-проверкой безопасности, индивидуальные настройки плагинов (`SETTINGS`) и интерактивная форма запуска каждого инструмента.\n"
        "- Два новых авторских плагина в комплекте: **`gguf_toolkit`** (`gguf_inspect` без VRAM, `gguf_quantize`, `gguf_benchmark`) и **`xtts_voice_studio`**.\n"
        "- Встроенная **локальная база знаний RAG** (индексация папок MD/TXT/PDF/DOCX/кода, BM25 + векторный поиск) и клиент внешних **MCP-серверов (Model Context Protocol)**.\n\n"
        "### 📊 5. Дашборд 2.0, Wi-Fi Кластер 2.0 (`ggml-rpc-server`), Темы и Персоны\n"
        "- **Дашборд 2.0:** 60-минутная история (1800 точек), интерактивные тултипы при наведении, энергопотребление GPU (`Вт`), `tok/s`, сеть/диск (`КБ/с`), таблица топ-процессов, экспорт телеметрии в CSV и полная адаптация под **Светлую тему (`☀️`)**.\n"
        "- **Wi-Fi Кластер 2.0:** фоновый авто-опрос сохранённых LAN LLM-узлов после перезапуска, распределённый RAG-скоринг и матричные операции на телефонах, а также 1-клик запуск нативного **`llama.cpp ggml-rpc-server`** для объединения VRAM нескольких ПК.\n"
        "- **UX Чата:** 6 ролей/персон агента, всплывающее меню слэш-команд (`/`), автоопределение режима цели из текста, редактирование/перегенерация сообщений, Windows Toast-уведомления и модуль самообновления.\n\n"
        "---\n\n"
        "### 📸 Скриншоты v1.0.8-beta.1\n"
        "![LoRA & Unsloth Studio 3.0](https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-train.png)\n"
        "![Голосовой созвон 2.0](https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-voice-call.png)\n"
        "![Встроенный IDE Редактор файлов](https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-files.png)\n"
        "![Дашборд системы 2.0 (Тёмная тема)](https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-dashboard.png)\n"
        "![Дашборд системы 2.0 (Светлая тема)](https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-light.png)\n"
        "![Wi-Fi Кластер 2.0 и llama.cpp RPC](https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-cluster.png)\n"
        "![Плагины 2.0 и GGUF Toolkit](https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-plugins.png)\n\n"
        "### 💿 Установка (Windows)\n"
        "- **Вариант A — установщик:** запустить `LocalHarness-Setup-v1.0.8-beta.1.exe` (без прав администратора; обновление сохраняет ваши `data`, `work` и `config.json`).\n"
        "- **Вариант B — portable:** распаковать `LocalHarness-v1.0.8-beta.1-windows.zip` и запустить `LocalHarness.exe`.\n"
    )

    repo = "lenovolquser15-ai/Local-AI-IDE"
    url = f"https://api.github.com/repos/{repo}/releases"

    # Check if release for tag already exists
    chk = requests.get(f"{url}/tags/{tag}", headers=headers, timeout=30)
    if chk.status_code == 200:
        rel = chk.json()
        print(f"Release {tag} already exists, updating body: {rel.get('html_url')}")
        r = requests.patch(
            f"{url}/{rel['id']}",
            headers=headers,
            json={"name": name, "body": body, "draft": False, "prerelease": True},
            timeout=30,
        )
        r.raise_for_status()
        rel = r.json()
    else:
        print(f"Creating pre-release {tag} on {repo}...")
        r = requests.post(
            url,
            headers=headers,
            json={
                "tag_name": tag,
                "target_commitish": "main",
                "name": name,
                "body": body,
                "draft": False,
                "prerelease": True,
            },
            timeout=30,
        )
        if r.status_code != 201:
            print("Failed to create release:", r.status_code, r.text)
            sys.exit(1)
        rel = r.json()

    rel_url = rel.get("html_url")
    print(f"Pre-release live at: {rel_url}")

    base_upload = rel["upload_url"].split("{")[0]
    existing_assets = {a["name"]: a["id"] for a in (rel.get("assets") or [])}

    assets = [
        (f"LocalHarness-Setup-{tag}.exe", os.path.join(ROOT, "dist", f"LocalHarness-Setup-{tag}.exe"), "application/octet-stream"),
        (f"LocalHarness-{tag}-windows.zip", os.path.join(ROOT, "dist", f"LocalHarness-{tag}-windows.zip"), "application/zip"),
    ]

    for fname, fpath, ctype in assets:
        if not os.path.exists(fpath):
            print(f"ERROR: Missing asset: {fpath}")
            sys.exit(1)
        if fname in existing_assets:
            requests.delete(f"{url}/assets/{existing_assets[fname]}", headers=headers, timeout=30)
        sz = os.path.getsize(fpath)
        print(f"Uploading {fname} ({sz:,} bytes)...")
        with open(fpath, "rb") as fh:
            up_resp = requests.post(
                f"{base_upload}?name={fname}",
                headers={
                    "Authorization": f"token {token}",
                    "Content-Type": ctype,
                    "User-Agent": "LocalHarness-Release-Script",
                },
                data=fh,
                timeout=900,
            )
        if up_resp.status_code not in (200, 201):
            print(f"Upload failed for {fname}:", up_resp.status_code, up_resp.text)
            sys.exit(1)
        print(f"Uploaded {fname}: {up_resp.json().get('browser_download_url')}")

    print("PRE_RELEASE_PUBLISHED_OK:", rel_url)


if __name__ == "__main__":
    main()

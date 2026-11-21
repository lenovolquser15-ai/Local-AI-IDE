import os
import subprocess
import sys
import requests


def get_token():
    p = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n",
        text=True,
        capture_output=True,
        check=True
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

    tag = "v1.0.7"
    name = "LocalHarness v1.0.7"
    body = (
        "**Telegram-канал:** [t.me/RemingOstai](https://t.me/RemingOstai)\n\n"
        "**Что нового в 1.0.7**\n"
        "- **🔥 Термозащита GPU и автоохлаждение:** мониторинг температуры видеокарты в реальном времени (`❄️ 42°C` → `⚠️ 88°C` → `🔥 101°C`), "
        "плавный термо-троттлинг скорости токенов при нагреве, а при экстремальной температуре (95–100°C+) — аварийное окно "
        "с выгрузкой модели из VRAM (LM Studio / Ollama) и таймером охлаждения на 2–N минут. В Настройках есть настройка порогов и кнопки симуляции.\n"
        "- **📊 Дашборд системы:** новая вкладка «Дашборд» с 6 карточками сводки и 4 живыми графиками на чистом HTML5 Canvas "
        "(температура GPU с линиями порогов, загрузка GPU, VRAM, RAM и CPU) за последние 2 / 5 / 10 минут.\n"
        "- **📱 Wi-Fi Кластер устройств:** новая вкладка «Кластер» позволяет подключать телефоны, планшеты и другие ПК в одной Wi-Fi сети "
        "по QR-коду (`/worker`, без установки приложений) с ползунком щадящей нагрузки (`1..N` потоков) и защитой батареи `< 20%`. "
        "Также умеет сканировать Wi-Fi подсеть на другие ПК с Ollama / LM Studio / llama.cpp и автоматически перебрасывать на них LLM-запросы при перегреве GPU!\n"
        "- **🕵️ Браузер Obscura (Rust stealth CDP) для поиска:** встроенная поддержка легковесного Rust headless-браузера "
        "[Obscura](https://github.com/h4ckf0r0day/obscura) (`--stealth`, 30–130 МБ RAM) как приоритетного или резервного stealth-движка "
        "в `web_search` и `browser` (настраивается в Настройки → Поиск).\n"
        "- **🎤 Голосовой режим (STT + TTS):** кнопка микрофона для диктовки запросов прямо в чат и кнопка озвучки ответов модели вслух.\n"
        "- **⏰ Планировщик и Автоматизации:** новая вкладка «Расписание» — запуск промптов агента и Shell-команд каждые N минут или ежедневно по времени `HH:MM` с историей запусков и просмотром чата.\n"
        "- **📦 Экспорт / Импорт профиля (`.zip` бэкап):** полный перенос настроек, плагинов, памяти, расписаний и сессий (`.jsonl`) на другой ПК в один клик (в Настройках).\n\n"
        "**Исправления после beta.1**\n"
        "- Точное определение загруженных в память моделей LM Studio (`/api/v1/models` и `/api/v0/models` — не показывает «Выгрузить», когда модель не загружена).\n"
        "- Синхронная выгрузка моделей по `identifier`, флаг `-y` при загрузке и поле поиска моделей на вкладке LM Studio.\n"
        "- Автозапуск сбора метрик дашборда и планировщика в режиме трея Windows (`desktop.py`) и перерисовка графиков при ресайзе.\n"
        "- Исправлены `POST`-запросы для очистки памяти и отмены охлаждения GPU, поддержка `.jsonl` в `.zip`-бэкапах, коллизии ID сессий и сохранение истории чата в фоновых задачах планировщика.\n\n"
        "**Тесты**\n"
        "- Все **199 автоматических тестов** (и 92 подтеста, `pytest tests/`) пройдены без ошибок.\n\n"
        "**Скриншоты**\n"
        "![Дашборд системы: живые Canvas-графики GPU, VRAM, RAM и CPU]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-dashboard.png)\n"
        "![Wi-Fi Кластер: подключение телефонов по QR-коду и LAN LLM-узлы]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-cluster.png)\n"
        "![Планировщик и Автоматизации: задачи по расписанию и журнал запусков]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-schedule.png)\n"
        "![Чат: индикатор температуры GPU и голосовой режим]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-chat.png)\n"
        "![Настройки: термозащита GPU, Obscura stealth-поиск и .zip бэкап]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-settings.png)\n\n"
        "**Установка (Windows)**\n"
        "- **Вариант A — установщик:** запустить `LocalHarness-Setup-v1.0.7.exe` (без прав администратора; "
        "обновление не трогает `data`, `work` и `config.json`).\n"
        "- **Вариант B — portable:** распаковать `LocalHarness-v1.0.7-windows.zip` и запустить `LocalHarness.exe`.\n\n"
        "Интерфейс откроется на http://127.0.0.1:7788.\n"
    )

    repo = "lenovolquser15-ai/Local-AI-IDE"
    url = f"https://api.github.com/repos/{repo}/releases"

    print(f"Creating release {tag} on {repo}...")
    r = requests.post(
        url,
        headers=headers,
        json={
            "tag_name": tag,
            "target_commitish": "main",
            "name": name,
            "body": body,
            "draft": False,
            "prerelease": False,
        },
        timeout=30,
    )
    if r.status_code != 201:
        print("Failed to create release:", r.status_code, r.text)
        sys.exit(1)

    rel = r.json()
    rel_url = rel.get("html_url")
    print(f"Pre-release created successfully: {rel_url}")

    base_upload = rel["upload_url"].split("{")[0]

    assets = [
        (f"LocalHarness-Setup-{tag}.exe", f"dist/LocalHarness-Setup-{tag}.exe", "application/octet-stream"),
        (f"LocalHarness-{tag}-windows.zip", f"dist/LocalHarness-{tag}-windows.zip", "application/zip"),
    ]

    for fname, fpath, ctype in assets:
        if not os.path.exists(fpath):
            print(f"Skipping missing asset: {fpath}")
            continue
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
                timeout=600,
            )
        if up_resp.status_code not in (200, 201):
            print(f"Upload failed for {fname}:", up_resp.status_code, up_resp.text)
            sys.exit(1)
        print(f"Uploaded {fname}: {up_resp.json().get('browser_download_url')}")

    print("ALL DONE!")


if __name__ == "__main__":
    main()

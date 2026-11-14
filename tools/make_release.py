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

    tag = "v1.0.4"
    name = "LocalHarness v1.0.4"
    body = (
        "Обновление 1.0.4: долговременная память, мобильный доступ по Wi-Fi, генерация картинок и игровой режим.\n\n"
        "**Telegram-канал:** [t.me/RemingOstai](https://t.me/RemingOstai)\n\n"
        "**Что нового**\n"
        "- **Долговременная память:** агент запоминает факты о пользователе и проекте между сессиями "
        "(`data/memory.json`). Записи добавляются в системный промпт; их видно и можно чистить в настройках.\n"
        "- **Мобильный доступ по Wi-Fi:** сервер можно поднять на `0.0.0.0`, интерфейс откроется с телефона по QR-коду. "
        "Клиенты не из локальной сети блокируются.\n"
        "- **Генерация картинок:** инструмент `generate_image` работает через SD WebUI или ComfyUI "
        "(локально, без облака). Нужен запущенный backend генерации.\n"
        "- **Игровой режим:** инструмент `game_control` — нажатия клавиш Win32 и относительный обзор мышью. "
        "Failsafe: `Escape`/`Pause` немедленно отпускают все зажатые клавиши.\n"
        "- **Провайдер Strata MoE:** поддержка сервера `127.0.0.1:8080/v1`.\n"
        "- Установщик: галочка «ярлык на рабочем столе» включена по умолчанию; из сборки исключены `__pycache__` и `.pyc`.\n\n"
        "**Исправлено**\n"
        "- **Неверный LAN-IP:** адрес мог определяться как VPN-адаптер (`172.19.0.1`). Теперь выбирается реальный "
        "Wi-Fi/Ethernet адрес (ранжирование по типу интерфейса, кэш на 15 секунд).\n"
        "- **Падение ленты после ответа без метрик:** `finishRun` падал на событии `done` без поля `speed`, "
        "статистика хода не отображалась.\n\n"
        "**Скриншоты**\n"
        "![Чат: сессии, чеклист цели, шкала контекста, раздумья, траектория]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-chat.png)\n"
        "![Настройки в шторке: память, генерация картинок, мобильный доступ]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-settings.png)\n"
        "![Мобильный доступ: адрес и QR-код]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-lan.png)\n"
        "![Вкладка плагинов: установленные и каталог]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-plugins.png)\n\n"
        "**Установка (Windows)**\n"
        "- **Вариант A — установщик:** запустить `LocalHarness-Setup-v1.0.4.exe`. Без прав администратора, ярлык на "
        "рабочем столе создаётся по умолчанию. Автозапуск — только если отметить галочку. Обновление и удаление не "
        "трогают `data`, `work` и `config.json`.\n"
        "- **Вариант B — portable:** распаковать `LocalHarness-v1.0.4-windows.zip` в папку без кириллицы в пути и "
        "запустить `LocalHarness.exe`.\n\n"
        "Нужен LM Studio с загруженной моделью (или Ollama); Python не требуется. Интерфейс откроется на http://127.0.0.1:7788.\n\n"
        "Сборка чистая: без личных данных, истории и конфига. Данные создаются рядом с exe при первом запуске.\n"
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
    print(f"Release created successfully: {rel_url}")

    base_upload = rel["upload_url"].split("{")[0]

    assets = [
        ("LocalHarness-Setup-v1.0.4.exe", "dist/LocalHarness-Setup-v1.0.4.exe", "application/octet-stream"),
        ("LocalHarness-v1.0.4-windows.zip", "dist/LocalHarness-v1.0.4-windows.zip", "application/zip"),
    ]

    for fname, fpath, ctype in assets:
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
                timeout=180,
            )
        if up_resp.status_code not in (200, 201):
            print(f"Failed to upload {fname}: {up_resp.status_code} {up_resp.text}")
            sys.exit(1)
        dl_url = up_resp.json().get("browser_download_url")
        print(f"Uploaded {fname} -> {dl_url}")

    print("\nRelease completed successfully!")

if __name__ == "__main__":
    main()

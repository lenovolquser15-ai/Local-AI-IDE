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

    tag = "v1.0.6"
    name = "LocalHarness v1.0.6"
    body = (
        "Обновление 1.0.6: любой GitHub-репозиторий превращается в плагин, Codex Security, "
        "Мастерская плагинов и строгая проверка контекста.\n\n"
        "**Telegram-канал:** [t.me/RemingOstai](https://t.me/RemingOstai)\n\n"
        "**Что нового**\n"
        "- **Конвертер репозиториев:** вкладка «Конвертер» делает из любого GitHub-репозитория рабочий "
        "плагин — Python-библиотеку, MCP-сервер, Node-пакет или CLI-утилиту. Находит манифесты, включая "
        "вложенные пакеты в монорепозиториях (у `openai/codex-security` — `sdk/typescript`), выбирает "
        "имя и точку входа и генерирует инструменты `<имя>_info`, `_files`, `_read` и `_cli`. "
        "Готовый файл проходит статический аудит и ставится в `plugins/`.\n"
        "- **Плагин Codex Security:** `plugins/codex_security.py` оборачивает CLI "
        "[openai/codex-security](https://github.com/openai/codex-security) — статус, скан путей/diff/deep, "
        "история сканов и прямой CLI. Если не хватает Node.js 22.13+ или `cs login`, инструменты честно "
        "объясняют, что доустановить.\n"
        "- **Мастерская и Конструктор:** вкладка «Плагины» теперь с четырьмя подвкладками — установленные, "
        "каталог сообщества, конвертер и конструктор. Публикация плагина в Мастерскую, установка в один "
        "клик, редактирование кода прямо в интерфейсе и тестовый вызов инструмента в песочнице "
        "(`harness/plugin_sandbox.py`).\n"
        "- **Аудит плагинов:** перед установкой и публикацией код проходит AST-скан (опасные "
        "`os.system`/`subprocess`/`eval`, сеть, ввод с клавиатуры) и получает бейдж «аудит кода: чисто». "
        "При заданном ключе VirusTotal дополнительно проверяется SHA-256 файла.\n"
        "- **Валидация контекстного окна:** интерфейс, сервер и агент зажимают `context_tokens` "
        "в 512–262144, а `max_tokens` — в 64–32768. Мусор и числа вне диапазона больше не проходят, "
        "предупреждение показывает фактически применённое значение.\n"
        "- **Удобная раскладка не-чатных вкладок:** плагины, конвертер и LM Studio занимают всю ширину "
        "окна — карточки не обрезаются, длинный заголовок сессии не ломает шапку.\n\n"
        "**Тесты**\n"
        "- `tests/test_converter_v2.py` — вложенные манифесты, выход за корень, опасные аргументы; "
        "`tests/test_workshop_and_converter.py` — Мастерская, аудит и установка. Полный прогон pytest "
        "и живой тест конвертера на `openai/codex-security` пройдены на собранном exe.\n\n"
        "**Скриншоты**\n"
        "![Чат: сессии, чеклист цели, шкала контекста, траектория]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-chat.png)\n"
        "![Настройки в шторке: память, удалённый доступ, LAN]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-settings.png)\n"
        "![Мобильный и удалённый доступ: адрес, временный код и QR]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-lan.png)\n"
        "![Вкладка плагинов: установленные, включая Codex Security, мастерская и каталог]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-plugins.png)\n"
        "![Конвертер: любой GitHub-репозиторий в плагин с живыми инструментами]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-converter.png)\n\n"
        "Скриншоты версии 1.0.5 сохранены в архиве: "
        "[docs/screenshots/v1.0.5](https://github.com/lenovolquser15-ai/Local-AI-IDE/tree/main/docs/screenshots/v1.0.5).\n\n"
        "**Установка (Windows)**\n"
        "- **Вариант A — установщик:** запустить `LocalHarness-Setup-v1.0.6.exe`. Без прав администратора, "
        "ярлык на рабочем столе создаётся по умолчанию. Автозапуск — только если отметить галочку. "
        "Обновление и удаление не трогают `data`, `work` и `config.json`.\n"
        "- **Вариант B — portable:** распаковать `LocalHarness-v1.0.6-windows.zip` в папку без кириллицы "
        "в пути и запустить `LocalHarness.exe`.\n\n"
        "Нужен LM Studio с загруженной моделью (или Ollama); Python не требуется. "
        "Интерфейс откроется на http://127.0.0.1:7788.\n\n"
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
        ("LocalHarness-Setup-v1.0.6.exe", "dist/LocalHarness-Setup-v1.0.6.exe", "application/octet-stream"),
        ("LocalHarness-v1.0.6-windows.zip", "dist/LocalHarness-v1.0.6-windows.zip", "application/zip"),
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

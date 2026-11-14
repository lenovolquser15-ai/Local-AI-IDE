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

    tag = "v1.0.3"
    name = "LocalHarness v1.0.3"
    body = (
        "Обновление 1.0.3: исправление статуса загрузки моделей LM Studio и удобная выгрузка из памяти.\n\n"
        "**Telegram-канал:** [t.me/RemingOstai](https://t.me/RemingOstai)\n\n"
        "**Что нового и исправлено**\n"
        "- **Исправление ложного статуса «модель не загружена»:**\n"
        "  - Добавлен прямой опрос REST API LM Studio (`/api/v0/models` и `/v1/models`), когда `lms ps` возвращает пустой список "
        "(при загрузке моделей через графический интерфейс LM Studio или JIT-режим сервера).\n"
        "  - Добавлена поддержка моделей с типом `vlm` / `vision` (например, Ministral).\n"
        "  - Нормализовано сопоставление путей и алиасов моделей в Windows (пути с обратными слэшами, суффиксы квантования).\n"
        "  - Уменьшен TTL кэша при пустом списке до 4 секунд, внедрена мгновенная инвалидация кэша статуса моделей "
        "при смене настроек, отправке сообщений в чат и выгрузке.\n"
        "- **Удобное управление выгрузкой моделей из памяти:**\n"
        "  - Кнопка быстрой выгрузки в шапке чата рядом со статусом готовности модели (`⏏ Выгрузить модель`).\n"
        "  - Кнопка выгрузки текущей модели в шторке «Настройки» в секции «Модель в памяти».\n"
        "  - Кнопка «Выгрузить» прямо на карточке модели из списка локальных моделей на диске (если модель загружена в RAM/VRAM).\n"
        "  - Кнопки быстрой выгрузки всех моделей во вкладке LM Studio и в блоке «В памяти».\n\n"
        "**Скриншоты**\n"
        "![Чат: сессии, чеклист цели, шкала контекста, раздумья, траектория]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-chat.png)\n"
        "![Настройки в шторке: провайдер, права, ссылка на баги]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-settings.png)\n"
        "![Вкладка плагинов: установленные и каталог]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-plugins.png)\n\n"
        "**Установка (Windows)**\n"
        "- **Вариант A — установщик:** запустить `LocalHarness-Setup-v1.0.3.exe`. Без прав администратора, автозапуск — "
        "только если отметить галочку. Обновление и удаление не трогают `data`, `work` и `config.json`.\n"
        "- **Вариант B — portable:** распаковать `LocalHarness-v1.0.3-windows.zip` в папку без кириллицы в пути и "
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
        ("LocalHarness-Setup-v1.0.3.exe", "dist/LocalHarness-Setup-v1.0.3.exe", "application/octet-stream"),
        ("LocalHarness-v1.0.3-windows.zip", "dist/LocalHarness-v1.0.3-windows.zip", "application/zip"),
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

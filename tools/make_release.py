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

    tag = "v1.0.5"
    name = "LocalHarness v1.0.5"
    body = (
        "Обновление 1.0.5: переработанное окно контекста и удалённый доступ через Cloudflare.\n\n"
        "**Telegram-канал:** [t.me/RemingOstai](https://t.me/RemingOstai)\n\n"
        "**Что нового**\n"
        "- **Честная шкала контекста:** три сегмента — схема инструментов, диалог и резерв под ответ; "
        "засечка порога сжатия, проценты от фактического окна и раздельные цифры `history/tools/answer` "
        "в событии `context`. После сжатия видно, сколько токенов было и стало.\n"
        "- **Компактная схема инструментов:** полный профиль ~4.1k токенов вместо ~6.1k, `core` — ~1.7k "
        "вместо ~2.4k. Все инструменты и параметры сохранены, сокращены только описания у очевидных полей. "
        "На окне 16k диалогу достаётся ~11.5k вместо ~9.5k; если на диалог остаётся меньше 4096 токенов, "
        "харнес сам включает `core`.\n"
        "- **Удалённый доступ через Cloudflare:** временный код, сайт ввода кода, подписанная HttpOnly-cookie "
        "и WebSocket-реле — белый IP и проброс портов не нужны. Есть QR, «Новый код» с ротацией epoch "
        "(прежние подключения сразу отключаются), автопродление кода и офлайн-страница, если ПК спит. "
        "Адреса реле и сайта задаются в настройках и разворачиваются скриптом `tools/deploy_cloudflare.py`.\n"
        "- **Camoufox как запасной движок поиска:** Firefox со скрытым отпечатком выручает, когда "
        "DuckDuckGo или Bing отдают капчу либо `403`.\n\n"
        "**Исправлено**\n"
        "- **WinError 6 в трее и автозапуске:** `lms`, `nslookup`, `nvidia-smi`, `ssh`/`scp` и фоновые "
        "процессы больше не падают на невалидном stdin в консольном запуске. Проверка готовности модели "
        "снова работает в собранном приложении.\n"
        "- **Обрыв потока модели:** ошибка теперь закрывает соединение и объясняет причину (переполнение "
        "контекста или выгрузка модели) вместо тихого зависания; остановка пользователем не маскируется "
        "под сбой.\n\n"
        "**Скриншоты**\n"
        "![Чат: сессии, чеклист цели, шкала контекста (схема/диалог/ответ), траектория]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-chat.png)\n"
        "![Настройки в шторке: память, удалённый доступ, LAN]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-settings.png)\n"
        "![Мобильный и удалённый доступ: адрес, временный код и QR]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-lan.png)\n"
        "![Вкладка плагинов: установленные и каталог]"
        "(https://raw.githubusercontent.com/lenovolquser15-ai/Local-AI-IDE/main/docs/screenshots/ui-plugins.png)\n\n"
        "Скриншоты версии 1.0.4 сохранены в архиве: "
        "[docs/screenshots/v1.0.4](https://github.com/lenovolquser15-ai/Local-AI-IDE/tree/main/docs/screenshots/v1.0.4).\n\n"
        "**Установка (Windows)**\n"
        "- **Вариант A — установщик:** запустить `LocalHarness-Setup-v1.0.5.exe`. Без прав администратора, "
        "ярлык на рабочем столе создаётся по умолчанию. Автозапуск — только если отметить галочку. "
        "Обновление и удаление не трогают `data`, `work` и `config.json`.\n"
        "- **Вариант B — portable:** распаковать `LocalHarness-v1.0.5-windows.zip` в папку без кириллицы "
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
        ("LocalHarness-Setup-v1.0.5.exe", "dist/LocalHarness-Setup-v1.0.5.exe", "application/octet-stream"),
        ("LocalHarness-v1.0.5-windows.zip", "dist/LocalHarness-v1.0.5-windows.zip", "application/zip"),
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

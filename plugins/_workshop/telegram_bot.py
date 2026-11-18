"""Telegram Bot Integration Plugin for LocalHarness.

Allows the AI agent to send messages, alerts, and poll updates from Telegram bots.
"""
import requests

NAME = "telegram_bot"


def _tg_send(cfg, args):
    token = args.get("token") or cfg.get("telegram_bot_token")
    if not token:
        return {"ok": False, "error": "нужен токен бота (token или config.telegram_bot_token)"}
    chat_id = args.get("chat_id")
    if not chat_id:
        return {"ok": False, "error": "нужен chat_id получателя или канала"}
    text = args.get("text") or ""
    if not text:
        return {"ok": False, "error": "текст сообщения пуст"}

    url = f"https://api.telegram.org/bot{token.strip()}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": args.get("parse_mode", "HTML")}
    try:
        r = requests.post(url, json=payload, timeout=15)
        res = r.json()
        if res.get("ok"):
            return {"ok": True, "message_id": res.get("result", {}).get("message_id"), "chat_id": chat_id}
        return {"ok": False, "error": f"Telegram API error: {res.get('description')}"}
    except Exception as exc:
        return {"ok": False, "error": f"Ошибка сети: {exc}"}


def _tg_updates(cfg, args):
    token = args.get("token") or cfg.get("telegram_bot_token")
    if not token:
        return {"ok": False, "error": "нужен токен бота (token или config.telegram_bot_token)"}
    limit = int(args.get("limit") or 5)
    url = f"https://api.telegram.org/bot{token.strip()}/getUpdates"
    try:
        r = requests.get(url, params={"limit": limit}, timeout=15)
        res = r.json()
        if res.get("ok"):
            items = []
            for u in res.get("result", []):
                msg = u.get("message") or {}
                items.append({
                    "update_id": u.get("update_id"),
                    "from": msg.get("from", {}).get("username"),
                    "chat_id": msg.get("chat", {}).get("id"),
                    "text": msg.get("text"),
                })
            return {"ok": True, "count": len(items), "updates": items}
        return {"ok": False, "error": res.get("description")}
    except Exception as exc:
        return {"ok": False, "error": f"Ошибка сети: {exc}"}


TOOLS = [
    {
        "name": "tg_send_message",
        "desc": "Отправить сообщение в Telegram пользователю или в канал через бота",
        "params": {
            "text": ("string", "текст сообщения"),
            "chat_id": ("string", "ID чата или @username канала"),
            "token": ("string?", "опциональный токен бота (если не задан в config)"),
            "parse_mode": ("string?", "HTML или Markdown"),
        },
        "required": ["text", "chat_id"],
        "fn": _tg_send,
    },
    {
        "name": "tg_get_updates",
        "desc": "Прочитать последние входящие сообщения бота из Telegram",
        "params": {
            "token": ("string?", "опциональный токен бота"),
            "limit": ("number?", "количество сообщений (до 20)"),
        },
        "required": [],
        "fn": _tg_updates,
    },
]

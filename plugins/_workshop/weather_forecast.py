"""Live Weather and Forecast Plugin for LocalHarness.

Provides real-time weather information and short-term forecasts for any city.
"""
import requests

NAME = "weather_forecast"


def _weather_get(cfg, args):
    city = (args.get("city") or "Moscow").strip()
    url = f"https://wttr.in/{city}?format=j1"
    headers = {"User-Agent": "curl/7.88.1"}
    try:
        r = requests.get(url, headers=headers, timeout=12)
        data = r.json()
        current = (data.get("current_condition") or [{}])[0]
        weather_desc = (current.get("lang_ru") or current.get("weatherDesc") or [{}])[0].get("value")
        return {
            "ok": True,
            "city": city,
            "temp_c": current.get("temp_C"),
            "feels_like_c": current.get("FeelsLikeC"),
            "humidity_pct": current.get("humidity"),
            "wind_speed_kmh": current.get("windspeedKmph"),
            "description": weather_desc,
        }
    except Exception as exc:
        return {"ok": False, "error": f"Ошибка получения погоды: {exc}"}


TOOLS = [
    {
        "name": "weather_get",
        "desc": "Узнать текущую погоду, температуру и влажность в указанном городе",
        "params": {
            "city": ("string", "название города (например: Москва, London, Tokyo)"),
        },
        "required": ["city"],
        "fn": _weather_get,
    },
]

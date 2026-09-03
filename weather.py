"""Weather helper — Open-Meteo with wttr.in fallback."""
from __future__ import annotations

import logging

import httpx

import config

log = logging.getLogger("spt.weather")

WMO = {
    0: "ясно",
    1: "преим. ясно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "туман",
    51: "морось",
    53: "морось",
    55: "морось",
    61: "небольшой дождь",
    63: "дождь",
    65: "сильный дождь",
    71: "снег",
    73: "снег",
    75: "сильный снег",
    80: "ливень",
    81: "ливень",
    82: "сильный ливень",
    95: "гроза",
}


def _fmt(temp, desc: str, wind=None) -> str:
    bits = [f"🌤 Погода в {config.WEATHER_CITY}: {desc}"]
    if temp is not None:
        bits.append(f"{float(temp):.0f}°C")
    if wind is not None:
        bits.append(f"ветер {float(wind):.0f} км/ч")
    return ", ".join(bits)


async def _open_meteo() -> str | None:
    headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
    # New API
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={config.WEATHER_LAT}&longitude={config.WEATHER_LON}"
        "&current=temperature_2m,weather_code,wind_speed_10m"
        "&timezone=auto"
    )
    async with httpx.AsyncClient(timeout=20.0, headers=headers, follow_redirects=True) as client:
        r = await client.get(url)
        if r.status_code == 200:
            cur = (r.json() or {}).get("current") or {}
            if cur.get("temperature_2m") is not None:
                code = int(cur.get("weather_code") or 0)
                return _fmt(
                    cur.get("temperature_2m"),
                    WMO.get(code, "без осадков"),
                    cur.get("wind_speed_10m"),
                )
        # Legacy current_weather=
        url2 = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={config.WEATHER_LAT}&longitude={config.WEATHER_LON}"
            "&current_weather=true"
        )
        r2 = await client.get(url2)
        r2.raise_for_status()
        cw = (r2.json() or {}).get("current_weather") or {}
        if cw.get("temperature") is None:
            return None
        code = int(cw.get("weathercode") or 0)
        return _fmt(cw.get("temperature"), WMO.get(code, "без осадков"), cw.get("windspeed"))


async def _wttr() -> str | None:
    # Compact text: "Кемерово: +5°C, Clear"
    url = "https://wttr.in/Kemerovo?format=%t+%C&lang=ru"
    headers = {"User-Agent": "curl/8.0"}
    async with httpx.AsyncClient(timeout=20.0, headers=headers, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        text = (r.text or "").strip()
        if not text or "Unknown" in text:
            return None
        return f"🌤 Погода в {config.WEATHER_CITY}: {text}"


async def kemerovo_weather_line() -> str:
    errors: list[str] = []
    for name, fn in (("open-meteo", _open_meteo), ("wttr", _wttr)):
        try:
            line = await fn()
            if line:
                return line
            errors.append(f"{name}: empty")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}")
            log.warning("weather %s failed: %s", name, exc)
    log.error("weather all failed: %s", "; ".join(errors))
    return f"🌤 Погода в {config.WEATHER_CITY}: временно недоступна"


def format_morning(greeting: str, weather: str, schedule_html: str) -> str:
    return f"{greeting}\n{weather}\n\n{schedule_html}"

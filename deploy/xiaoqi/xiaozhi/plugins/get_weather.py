"""get_weather 的替代实现：数据源换成 wttr.in（免费、无需 key、不需要专属 Host）。

原实现依赖和风天气，但新版 key 必须绑定账号专属的 api_host（默认那个公共 key 已经 401），
而 wttr.in 直接吃中文地名，一次请求就把实时天气带回来，够 LLM 说一句人话。
"""
import urllib.parse

import httpx

from config.logger import setup_logging
from plugins_func.register import Action, ActionResponse, ToolType, register_function
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

GET_WEATHER_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "获取某个地点的实时天气。用户说“杭州天气”时 location 传“杭州”。"
            "用户没提地点时才省略 location。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "地点名，例如杭州"},
                "lang": {"type": "string", "description": "语言 code，默认 zh_CN"},
            },
            "required": ["lang"],
        },
    },
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36"
    )
}
TIMEOUT = 8.0


# wttr.in 的天气描述只有英文，这里给常用项配上中文；没配到的保留原文（模型也能懂）
_CN_WEATHER = {
    "sunny": "晴", "clear": "晴", "partly cloudy": "多云", "cloudy": "多云",
    "overcast": "阴", "mist": "薄雾", "fog": "有雾", "freezing fog": "冻雾",
    "patchy rain nearby": "局部有小雨", "patchy rain possible": "可能有小雨",
    "light rain": "小雨", "light drizzle": "毛毛雨", "moderate rain": "中雨",
    "heavy rain": "大雨", "light rain shower": "阵雨", "moderate or heavy rain shower": "中到大雨",
    "thundery outbreaks possible": "可能有雷阵雨", "patchy light rain with thunder": "雷阵雨",
    "light snow": "小雪", "moderate snow": "中雪", "heavy snow": "大雪",
    "patchy snow possible": "可能有雪", "blizzard": "暴雪", "sleet": "雨夹雪",
    "patchy light drizzle": "零星毛毛雨", "moderate rain at times": "间歇中雨",
}

_CN_WIND = {"N": "北", "NNE": "北偏东", "NE": "东北", "ENE": "东偏北", "E": "东",
            "ESE": "东偏南", "SE": "东南", "SSE": "南偏东", "S": "南", "SSW": "南偏西",
            "SW": "西南", "WSW": "西偏南", "W": "西", "WNW": "西偏北", "NW": "西北",
            "NNW": "北偏西", "C": "静风"}


def _first(value, default=""):
    """wttr.in 的字段是 [{"value": "..."}] 形状，取第一个值。"""
    if isinstance(value, list) and value:
        item = value[0]
        if isinstance(item, dict):
            return str(item.get("value") or default).strip()
        return str(item).strip()
    return default


async def _fetch(location: str) -> dict | None:
    url = f"https://wttr.in/{urllib.parse.quote(location)}?format=j1&lang=zh"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers=HEADERS)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.bind(tag=TAG).error(f"wttr.in 请求失败({location}): {exc}")
        return None


def _describe(data: dict, fallback: str) -> str:
    cur = (data.get("current_condition") or [{}])[0]
    area = ((data.get("nearest_area") or [{}])[0])
    city = fallback if any("\u4e00" <= ch <= "\u9fff" for ch in fallback) else (
        _first(area.get("areaName"), fallback) or fallback)
    raw_desc = (_first(cur.get("weatherDesc")) or "未知天气").strip()
    desc = _CN_WEATHER.get(raw_desc.lower(), raw_desc)
    temp = cur.get("temp_C", "?")
    feels = cur.get("FeelsLikeC", "?")
    humidity = cur.get("humidity", "?")
    wind_kmph = cur.get("windspeedKmph", "?")
    wind_dir_cn = _CN_WIND.get(str(cur.get("winddir16Point", "")).upper(), "")

    parts = [f"{city}现在{desc}，气温{temp}度，体感{feels}度，湿度{humidity}%"]
    if wind_kmph not in ("", "?"):
        parts.append(f"{wind_dir_cn}风{wind_kmph}公里每小时")
    text = "，".join(parts) + "。"

    days = data.get("weather") or []
    if days:
        today = days[0]
        lo, hi = today.get("mintempC"), today.get("maxtempC")
        if lo and hi:
            text += f"今天{lo}到{hi}度。"
    return text


@register_function("get_weather", GET_WEATHER_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def get_weather(conn: "ConnectionHandler", location: str = None, lang: str = "zh_CN"):
    weather_config = conn.config.get("plugins", {}).get("get_weather", {})
    default_location = weather_config.get("default_location", "北京")
    target = (location or "").strip() or default_location

    data = await _fetch(target)
    if not data:
        # 中文地名有时 wttr.in 认不出，退回默认城市再试一次
        if target != default_location:
            data = await _fetch(default_location)
        if not data:
            return ActionResponse(Action.REQLLM, "天气服务暂时查不到，过一会儿再问我。", None)

    return ActionResponse(Action.REQLLM, _describe(data, target), None)

"""Разовая проверка: чем заменить запрос-название-города для общей проходки.

Запрос «Борисов» не зависит от точки (всегда одни и те же 17 организаций),
поэтому общая проходка по сетке на нём бесполезна. Нужен запрос, который
возвращает в точке организации разных рубрик.
"""

import json
import re
from urllib.parse import quote, urlencode

from curl_cffi import requests

STATE_RE = re.compile(
    r'<script type="application/json" class="state-view">(.*?)</script>', re.S
)

POINTS = {
    "центр": (28.485, 54.2277),
    "юг": (28.520, 54.200),
    "север": (28.440, 54.260),
    "запад": (28.400, 54.230),
}

CANDIDATES = [
    "услуги",
    "магазин",
    "организации",
    "компания",
    "магазины",
    "производство",
]


def orgs(html: str) -> list[dict]:
    state = json.loads(STATE_RE.search(html).group(1))
    found: list[dict] = []
    seen: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            key = str(node.get("id") or "")
            if key and key not in seen and (node.get("shortTitle") or node.get("title")):
                if node.get("coordinates"):
                    seen.add(key)
                    found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(state)
    return found


def first_category(node: dict) -> str:
    for item in node.get("categories") or []:
        if isinstance(item, dict) and item.get("name"):
            return item["name"]
    return ""


for query_text in CANDIDATES:
    query = quote(query_text, safe="")
    union: dict[str, dict] = {}
    per_point = []
    for lon, lat in POINTS.values():
        params = urlencode({"ll": f"{lon:.6f},{lat:.6f}", "z": "15", "page": "1"})
        url = f"https://yandex.by/maps/157/borisov/search/{query}/?{params}"
        response = requests.get(url, impersonate="chrome", timeout=25)
        items = orgs(response.text)
        per_point.append(len(items))
        for item in items:
            union.setdefault(str(item["id"]), item)
    categories = {first_category(item) for item in union.values()}
    categories.discard("")
    print(
        f"{query_text:15} по точкам: {per_point}, уникальных: {len(union):3}, "
        f"разных рубрик: {len(categories):3}"
    )
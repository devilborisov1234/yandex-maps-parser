"""Разовая проверка: зависит ли выдача карт от координаты точки ll."""

import json
import re
from urllib.parse import quote, urlencode

from curl_cffi import requests

STATE_RE = re.compile(
    r'<script type="application/json" class="state-view">(.*?)</script>', re.S
)


def org_ids(html: str) -> set[str]:
    state = json.loads(STATE_RE.search(html).group(1))
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            if (node.get("shortTitle") or node.get("title")) and node.get("id"):
                found.add(str(node["id"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(state)
    return found


# Три точки: центр Борисова, юг и север города (радиус сетки 7 км)
POINTS = {
    "центр": (28.485, 54.2277),
    "юг": (28.520, 54.200),
    "север": (28.440, 54.260),
    "запад": (28.400, 54.230),
}

for query_text in ("стоматология", "Борисов", "кафе"):
    query = quote(query_text, safe="")
    print(f"--- запрос: {query_text}")
    results = {}
    for label, (lon, lat) in POINTS.items():
        params = urlencode({"ll": f"{lon:.6f},{lat:.6f}", "z": "15", "page": "1"})
        url = f"https://yandex.by/maps/157/borisov/search/{query}/?{params}"
        response = requests.get(url, impersonate="chrome", timeout=25)
        ids = org_ids(response.text)
        results[label] = ids
        print(f"  {label:7} {lon},{lat}: {len(ids)} организаций")

    sets = list(results.values())
    common = set.intersection(*sets) if sets else set()
    union = set.union(*sets) if sets else set()
    print(f"  совпадают во всех точках: {len(common)}, всего уникальных: {len(union)}")
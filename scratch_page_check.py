"""Разовая проверка: работает ли параметр page в SSR-выдаче карт."""

import json
import re

from curl_cffi import requests
from urllib.parse import quote, urlencode

STATE_RE = re.compile(
    r'<script type="application/json" class="state-view">(.*?)</script>', re.S
)


def orgs(html: str) -> list[str]:
    state = json.loads(STATE_RE.search(html).group(1))
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if (node.get("shortTitle") or node.get("title")) and node.get("id"):
                found.append(str(node["id"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(state)
    return found


query = quote("кафе", safe="")
for page in (1, 2, 3):
    params = urlencode({"ll": "28.485000,54.227700", "z": "15", "page": str(page)})
    url = f"https://yandex.by/maps/157/borisov/search/{query}/?{params}"
    response = requests.get(url, impersonate="chrome", timeout=25)
    ids = orgs(response.text)
    print(f"page={page} status={response.status_code} orgs={len(ids)} first={ids[:3]}")

# Тот же адрес, но параметр страницы в имени, как это делает сам Яндекс
params = urlencode({"ll": "28.485000,54.227700", "z": "15"})
url = f"https://yandex.by/maps/157/borisov/search/{query}/?{params}"
response = requests.get(url, impersonate="chrome", timeout=25)
print(f"без page: status={response.status_code} orgs={len(orgs(response.text))}")
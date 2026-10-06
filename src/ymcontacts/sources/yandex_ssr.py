"""Яндекс Карты: сбор через серверный рендеринг страницы поиска.

Почему так, а не через внутренний API — это главное решение проекта.

Внутренний API карт (`/maps/api/search`) в 2026 году отдаёт вместо данных
объект `{"csrfToken": ...}` и требует подписанный запрос. Разбор такой схемы
ломается молча и в худший момент, а поддерживать её приходится вручную.

Поэтому берём то, что сервер отдаёт сам: страницу поиска
`/maps/157/<город>/search/<запрос>/?ll=<долгота>,<широта>&page=N`.
Она приходит уже с готовыми организациями — в JSON-блоке состояния внутри
HTML, в теге `<script type="application/json" class="state-view">`.

Что это даёт:
  * данные приходят в структурированном виде, без разбора вёрстки;
  * не нужен браузер, не нужны подписи и токены;
  * пагинация и координаты работают предсказуемо.

Что приходится принимать как ограничение: выдача зависит от точки `ll`, а не от
прямоугольника, и зум на состав выдачи не влияет. Поэтому покрытие города
делается сеткой точек с перекрытием, а не сеткой прямоугольников.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from .. import rawcache
from ..config import settings
from ..fetcher import HttpSession, SchemaError
from ..logging_setup import get_logger
from ..models import OrgCard, SearchPage
from ..normalize import clean_url, normalize_phone, org_key
from .base import Source

log = get_logger(__name__)

SOURCE_NAME = "yandex"

# Контейнер состояния страницы. Разметка стабильная: это единственный тег,
# в котором сервер кладёт данные, и он не меняется вместе с вёрсткой.
STATE_RE = re.compile(
    r'<script type="application/json" class="state-view">(.*?)</script>', re.S
)

# Сегмент города в адресе /maps/157/<город>/search/... обязателен: без него
# Яндекс отвечает 404 (проверено). Содержимое сегмента на результат не влияет —
# поиск идёт по координатам ll, — но сам город должен быть настоящим.
# По умолчанию берём транслитерацию названия города из настроек.
CITY_SEGMENT = ""

# Транслитерация кириллицы в латиницу по ГОСТ-подобной схеме Яндекса.
# Нужна только для адреса страницы; на данные поиска не влияет.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def city_segment() -> str:
    """Сегмент города для адреса страницы поиска."""
    explicit = (settings.maps_city_segment or CITY_SEGMENT).strip()
    if explicit:
        return explicit
    name = (settings.city_name or "").strip().lower()
    return "".join(_TRANSLIT.get(ch, ch) for ch in name)


# Сколько страниц выдачи забирать за один запрос. На странице ~25 организаций,
# поэтому 2 страницы — до 50 организаций на запрос.
PAGES_PER_QUERY = 2


def build_url(text: str, lon: float, lat: float, page: int = 1, maps_host: str | None = None) -> str:
    """Собрать адрес страницы поиска.

    Из параметров важны два: ll — точка, вокруг которой ищет Яндекс,
    и page — номер страницы выдачи. Зум оставлен фиксированным: замеры
    показали, что на состав выдачи он не влияет.
    """
    from urllib.parse import quote, urlencode

    host = maps_host or settings.maps_host
    query = quote(text, safe="")
    url = f"{host}/maps/157/{city_segment()}/search/{query}/"
    params = {"ll": f"{lon:.6f},{lat:.6f}", "z": "15", "page": str(page)}

    return f"{url}?{urlencode(params)}"


def extract_state(html: str) -> dict:
    """Достать и разобрать JSON-блок состояния страницы."""
    match = STATE_RE.search(html)
    if not match:
        # Нет контейнера — это либо капча, либо серьёзная смена вёрстки.
        raise SchemaError(
            "на странице нет блока состояния state-view. "
            "Вероятно, изменилась разметка карт или показана проверка"
        )
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise SchemaError(f"состояние не разобралось: {exc}") from exc


def walk(node: Any) -> Iterator[dict]:
    """Обойти всё дерево состояния и выдать все словари."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def looks_like_org(node: dict) -> bool:
    """Организация ли это: есть название и хотя бы один контактный признак."""
    if not node.get("shortTitle") and not node.get("title"):
        return False
    markers = ("phones", "coordinates", "uri", "id", "fullAddress", "address", "urls")
    return any(node.get(marker) for marker in markers)


def _extract_coords(node: dict) -> tuple[float | None, float | None]:
    """Координаты: в состоянии лежат в поле coordinates как [долгота, широта]."""
    for key in ("coordinates", "point", "position"):
        value = node.get(key)
        if isinstance(value, dict):
            value = value.get("coordinates")
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            try:
                return float(value[1]), float(value[0])
            except (TypeError, ValueError):
                continue
    return None, None


def _extract_phones(node: dict) -> list[str]:
    """Телефоны приходят списком словарей с полями number/value."""
    phones: list[str] = []
    for item in node.get("phones") or []:
        if isinstance(item, str):
            raw = item
        elif isinstance(item, dict):
            # value — нормализованный номер, number — как его показывают
            raw = item.get("value") or item.get("number") or item.get("text") or ""
        else:
            continue
        phone = normalize_phone(raw)
        if phone and phone not in phones:
            phones.append(phone)
    return phones


def _extract_socials(node: dict) -> dict[str, str]:
    """Соцсети: socialLinks состоит из {type, href}."""
    socials: dict[str, str] = {}
    for item in node.get("socialLinks") or []:
        if not isinstance(item, dict):
            continue
        kind = (item.get("type") or "").strip().lower()
        href = clean_url(item.get("href") or "")
        if kind and href:
            socials.setdefault(kind, href)

    # Ссылка на официальный сайт организации
    for link in node.get("businessLinks") or []:
        if isinstance(link, dict) and link.get("type") == "self":
            href = clean_url(link.get("href") or "")
            if href:
                socials.setdefault("site", href)
    return socials


def _extract_categories(node: dict) -> list[str]:
    """Рубрики: основная первая, дальше родительские."""
    categories: list[str] = []
    for item in node.get("categories") or []:
        if isinstance(item, dict):
            name = (item.get("name") or "").strip()
            if name and name not in categories:
                categories.append(name)
    return categories


def org_page_url(uri: str, maps_host: str | None = None) -> str:
    """Адрес карточки организации на картах из её uri.

    В состоянии uri бывает двух видов:
      /maps/org/9876543210/reviews/   — сразу с номером организации;
      ...?oid=9876543210               — через параметр.
    Число между ними и есть идентификатор карточки.
    """
    if not uri:
        return ""
    host = maps_host or settings.maps_host
    digits = re.search(r"/org/(\d+)", uri) or re.search(r"oid=(\d+)", uri)
    if not digits:
        return ""
    return f"{host}/maps/org/{digits.group(1)}/"


def card_from_org(node: dict, maps_host: str | None = None) -> OrgCard | None:
    """Собрать карточку организации из узла состояния."""
    name = (node.get("shortTitle") or node.get("title") or "").strip()
    if not name:
        return None

    # Идентификатор: oid из uri надёжнее и не зависит от seoname
    uri = node.get("uri") or ""
    external_id = str(node.get("id") or "") or uri
    if not external_id:
        return None

    host = maps_host or settings.maps_host
    url = org_page_url(uri, host)

    lat, lon = _extract_coords(node)
    socials = _extract_socials(node)
    categories = _extract_categories(node)

    # Сайт организации лежит в urls[0]
    website = ""
    urls = node.get("urls") or []
    if isinstance(urls, list) and urls:
        website = clean_url(urls[0])
    if not website:
        website = socials.pop("site", "")

    rating_data = node.get("ratingData") or {}
    try:
        rating = float(rating_data["ratingValue"]) if rating_data.get("ratingValue") is not None else None
    except (TypeError, ValueError, KeyError):
        rating = None
    try:
        reviews = int(rating_data["reviewCount"]) if rating_data.get("reviewCount") is not None else None
    except (TypeError, ValueError, KeyError):
        reviews = None

    # chain нужен для склейки: сеть организаций с общим названием
    chain = node.get("chain") or {}
    group_source = chain.get("seoname") if isinstance(chain, dict) else None

    return OrgCard(
        source=SOURCE_NAME,
        external_id=external_id,
        name=name,
        category=categories[0] if categories else "",
        categories=categories,
        address=(node.get("fullAddress") or node.get("address") or "").strip(),
        phones=_extract_phones(node),
        website=website,
        socials=socials,
        lat=lat,
        lon=lon,
        hours=node.get("workingTimeText") or "",
        rating=rating,
        reviews_count=reviews,
        url=url,
        group_key=group_source or org_key(name),
    )


def parse_cards(payload: Any, maps_host: str | None = None) -> list[OrgCard]:
    """Разобрать состояние страницы в карточки организаций, без дублей."""
    cards: list[OrgCard] = []
    seen: set[str] = set()

    for node in walk(payload):
        if not looks_like_org(node):
            continue
        card = card_from_org(node, maps_host)
        if card and card.external_id not in seen:
            seen.add(card.external_id)
            cards.append(card)
    return cards


class YandexSource(Source):
    """Основной адаптер: страница поиска карт через HTTP."""

    name = SOURCE_NAME

    def __init__(self, session: HttpSession | None = None, use_cache: bool = True) -> None:
        self.session = session or HttpSession(
            referer=f"{settings.maps_host}/maps/",
            language=f"{settings.browser_locale},ru;q=0.9",
        )
        self.use_cache = use_cache

    def search(self, text: str, bbox: str | None = None, page: int = 1) -> SearchPage:
        """Одна страница выдачи по точке и запросу.

        bbox из сетки используется только как источник координаты центра:
        прямоугольник поиска по этому эндпоинту не передаётся.
        """
        lon, lat = settings.city_center_lon, settings.city_center_lat
        if bbox:
            lon, lat = center_of_bbox(bbox)

        url = build_url(text, lon, lat, page=page)
        cache_key = rawcache.make_cache_key(self.name, text, round(lon, 4), round(lat, 4), page)

        if self.use_cache:
            cached = rawcache.load_payload(cache_key, settings.raw_dir)
            if cached is not None:
                log.debug("кэш: %s", cache_key)
                return SearchPage(text=text, bbox=bbox, page=page, payload=cached, from_cache=True)

        html = self.session.get_text(url)
        state = extract_state(html)

        rawcache.save_payload(state, cache_key, settings.raw_dir)
        log.debug("сеть: %s (%s)", url, cache_key)
        return SearchPage(text=text, bbox=bbox, page=page, payload=state, from_cache=False)

    def cards(self, page: SearchPage) -> list[OrgCard]:
        """Разобрать выдачу в карточки организаций."""
        cards = parse_cards(page.payload)
        log.debug("из выдачи «%s» (стр. %s) карточек: %s", page.text, page.page, len(cards))
        return cards

    def close(self) -> None:
        """Закрыть HTTP-соединение."""
        self.session.close()


def center_of_bbox(bbox: str) -> tuple[float, float]:
    """Центр прямоугольника "lon1,lat1~lon2,lat2" -> (долгота, широта)."""
    try:
        south_west, north_east = bbox.split("~")
        lon1, lat1 = (float(v) for v in south_west.split(","))
        lon2, lat2 = (float(v) for v in north_east.split(","))
        return (lon1 + lon2) / 2, (lat1 + lat2) / 2
    except (ValueError, AttributeError):
        return settings.city_center_lon, settings.city_center_lat
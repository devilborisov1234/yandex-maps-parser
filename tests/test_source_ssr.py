"""Тесты адаптера страницы поиска Яндекс Карт (SSR, без браузера).

Проверяем ровно то, на чём держится сбор: адрес страницы, извлечение
состояния из HTML, разбор карточки и то, что повторный запрос не ходит в сеть.
"""

from __future__ import annotations

import json

import pytest

from ymcontacts.config import Settings, settings
from ymcontacts.fetcher import SchemaError
from ymcontacts.sources import yandex_ssr
from ymcontacts.sources.yandex_ssr import (
    YandexSource,
    build_url,
    card_from_org,
    city_segment,
    extract_state,
    looks_like_org,
    parse_cards,
)

# Узел организации в том виде, в каком он лежит в состоянии страницы
ORG_NODE = {
    "id": "12345",
    "type": "Org",
    "shortTitle": "Дентал",
    "title": "Стоматологическая клиника «Дентал»",
    "seoname": "dental",
    "uri": "/maps/org/9876543210/reviews/",
    "fullAddress": "Минская область, Борисов, Госпитальная улица, 34",
    "address": "Госпитальная улица, 34",
    "phones": [
        {"type": "phone", "number": "+375 29 603-36-56", "value": "+375296033656"},
        {"type": "phone", "number": "8 (017) 333-33-33", "value": "+3751733333333"},
        {"type": "phone", "number": "дубль", "value": "+375296033656"},
        {"type": "phone", "number": "мусор", "value": ""},
    ],
    "urls": ["https://shtv-dental.103.by/?utm_source=yandex"],
    "socialLinks": [
        {"type": "viber", "href": "https://viber.click/375296033656"},
        {"type": "instagram", "href": "https://www.instagram.com/dental.by"},
    ],
    "categories": [
        {"name": "Стоматологическая клиника", "seoname": " stomatologiya"},
        {"name": "Медицинские организации", "seoname": "meditsina"},
    ],
    "coordinates": [28.475642, 54.240205],
    "ratingData": {"ratingValue": 4.7, "reviewCount": 132},
    "workingTimeText": "пн-пт 09:00-19:00",
    "isClosed": False,
    "geoId": "162167956",
    "chain": {"seoname": "dental", "name": "Дентал"},
}


def wrap_state(payload: dict) -> str:
    """Состояние в том виде, в каком оно лежит в HTML страницы."""
    return (
        "<!DOCTYPE html><html><head><title>Карты</title></head><body>"
        '<script type="application/json" class="state-view">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script></body></html>"
    )


class TestCitySegment:
    """Сегмент города в адресе обязателен — без него Яндекс отвечает 404."""

    def test_transliterated_from_city_name(self, monkeypatch):
        monkeypatch.setattr(settings, "city_name", "Борисов")
        monkeypatch.setattr(settings, "maps_city_segment", "")
        assert city_segment() == "borisov"

    def test_explicit_value_wins(self, monkeypatch):
        monkeypatch.setattr(settings, "city_name", "Борисов")
        monkeypatch.setattr(settings, "maps_city_segment", "Minsk")
        assert city_segment() == "Minsk"

    def test_latin_name_kept_as_is(self, monkeypatch):
        monkeypatch.setattr(settings, "city_name", "Minsk")
        monkeypatch.setattr(settings, "maps_city_segment", "")
        # сегменты городов у Яндекса в нижнем регистре
        assert city_segment() == "minsk"


class TestBuildUrl:
    def test_contains_city_segment_and_point(self, monkeypatch):
        monkeypatch.setattr(settings, "maps_city_segment", "borisov")
        url = build_url("стоматология", 28.5, 54.23, page=1)
        assert url.startswith("https://yandex.by/maps/157/borisov/search/")
        # кириллица запроса экранирована
        assert "%D1%81%D1%82%D0%BE" in url
        assert "ll=28.500000%2C54.230000" in url

    def test_page_goes_into_query(self, monkeypatch):
        monkeypatch.setattr(settings, "maps_city_segment", "borisov")
        url = build_url("аптека", 28.5, 54.23, page=3)
        assert url.endswith("page=3")

    def test_pages_produce_different_urls(self, monkeypatch):
        monkeypatch.setattr(settings, "maps_city_segment", "borisov")
        first = build_url("аптека", 28.5, 54.23, page=1)
        second = build_url("аптека", 28.5, 54.23, page=2)
        assert first != second

    def test_custom_host(self):
        url = build_url("аптека", 28.5, 54.23, maps_host="https://yandex.ru")
        assert url.startswith("https://yandex.ru/maps/157/")


class TestExtractState:
    def test_state_taken_from_html(self):
        payload = {"config": {"a": 1}, "items": [ORG_NODE]}
        state = extract_state(wrap_state(payload))
        assert state == payload

    def test_cyrillic_survives(self):
        state = extract_state(wrap_state({"items": [ORG_NODE]}))
        assert state["items"][0]["shortTitle"] == "Дентал"

    def test_missing_state_raises_schema_error(self):
        with pytest.raises(SchemaError):
            extract_state("<html><body>Проверка: введите код</body></html>")

    def test_broken_json_raises_schema_error(self):
        broken = (
            '<script type="application/json" class="state-view">{"a": </script>'
        )
        with pytest.raises(SchemaError):
            extract_state(broken)

    def test_wrong_class_ignored(self):
        # Похожий тег, но другой класс — состояние брать не откуда
        html = '<script type="application/json" class="other">{"a": 1}</script>'
        with pytest.raises(SchemaError):
            extract_state(html)


class TestOrgNodeDetection:
    def test_org_is_recognised(self):
        assert looks_like_org(ORG_NODE)

    def test_nested_advert_block_is_not_org(self):
        assert not looks_like_org({"title": "Реклама", "text": "купите"})

    def test_seo_breadcrumb_is_not_org(self):
        assert not looks_like_org({"title": "Стоматология", "href": "/maps/"})


class TestCardFromOrg:
    def test_basic_fields(self):
        card = card_from_org(ORG_NODE)
        assert card is not None
        assert card.name == "Дентал"
        assert card.external_id == "12345"
        assert card.category == "Стоматологическая клиника"
        assert card.categories == ["Стоматологическая клиника", "Медицинские организации"]
        assert card.address == "Минская область, Борисов, Госпитальная улица, 34"

    def test_phones_normalized_and_deduped(self):
        card = card_from_org(ORG_NODE)
        # мусорный номер отброшен, дубль не повторяется
        assert card.phones == ["+375296033656", "+3751733333333"]

    def test_website_without_tracking(self):
        card = card_from_org(ORG_NODE)
        assert card.website == "https://shtv-dental.103.by"

    def test_socials_collected(self):
        card = card_from_org(ORG_NODE)
        assert card.socials["viber"] == "https://viber.click/375296033656"
        assert card.socials["instagram"] == "https://www.instagram.com/dental.by"

    def test_coordinates_swapped_to_lat_lon(self):
        card = card_from_org(ORG_NODE)
        # в состоянии [долгота, широта], в карточке — широта, долгота
        assert card.lat == pytest.approx(54.240205)
        assert card.lon == pytest.approx(28.475642)

    def test_rating_and_reviews(self):
        card = card_from_org(ORG_NODE)
        assert card.rating == pytest.approx(4.7)
        assert card.reviews_count == 132

    def test_chain_used_for_merging(self):
        card = card_from_org(ORG_NODE)
        assert card.group_key == "dental"

    def test_maps_page_url_built_from_uri(self):
        card = card_from_org(ORG_NODE)
        assert card.url == "https://yandex.by/maps/org/9876543210/"

    def test_node_without_id_and_name_skipped(self):
        assert card_from_org({"phones": [{"value": "+375291111111"}]}) is None

    def test_site_taken_from_business_links(self):
        node = dict(ORG_NODE)
        node["urls"] = []
        node["businessLinks"] = [{"type": "self", "href": "https://dental.by"}]
        card = card_from_org(node)
        assert card.website == "https://dental.by"

    def test_broken_rating_does_not_break_card(self):
        node = dict(ORG_NODE)
        node["ratingData"] = {"ratingValue": "нет", "reviewCount": "много"}
        card = card_from_org(node)
        assert card.rating is None
        assert card.reviews_count is None


class TestParseCards:
    """Список организаций лежит глубоко в состоянии, путь к нему меняется."""

    def test_finds_orgs_at_known_depth(self):
        state = {"config": {}, "groups": [{"items": [ORG_NODE]}]}
        cards = parse_cards(state)
        assert [card.name for card in cards] == ["Дентал"]

    def test_finds_orgs_at_unknown_depth(self):
        state = {"a": {"b": {"c": {"d": {"e": [{"f": ORG_NODE}]}}}}}
        cards = parse_cards(state)
        assert len(cards) == 1

    def test_same_org_twice_is_one_card(self):
        state = {"one": [ORG_NODE], "two": [dict(ORG_NODE)]}
        assert len(parse_cards(state)) == 1

    def test_empty_state_gives_nothing(self):
        assert parse_cards({"config": {"spinner": "loading"}}) == []

    def test_unexpected_list_shape_does_not_crash(self):
        state = {"items": [None, 42, "строка", {}, [ORG_NODE]]}
        assert len(parse_cards(state)) == 1


class TestSearch:
    """Поиск не должен ходить в сеть повторно и должен отдавать карточки."""

    @pytest.fixture(autouse=True)
    def _tmp_settings(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "raw_dir", tmp_path)
        monkeypatch.setattr(settings, "maps_city_segment", "borisov")

    class _FakeSession:
        def __init__(self, html: str) -> None:
            self.html = html
            self.calls: list[str] = []

        def get_text(self, url: str) -> str:
            self.calls.append(url)
            return self.html

        def close(self) -> None:
            pass

    def test_first_call_fetches_second_uses_cache(self):
        session = self._FakeSession(wrap_state({"items": [ORG_NODE]}))
        source = YandexSource(session=session, use_cache=True)

        first = source.search("стоматология", bbox="28.4,54.2~28.5,54.3", page=1)
        assert first.from_cache is False
        second = source.search("стоматология", bbox="28.4,54.2~28.5,54.3", page=1)
        assert second.from_cache is True
        # сеть тронута ровно один раз
        assert len(session.calls) == 1

    def test_pages_do_not_share_cache(self):
        session = self._FakeSession(wrap_state({"items": [ORG_NODE]}))
        source = YandexSource(session=session, use_cache=True)
        source.search("стоматология", page=1)
        source.search("стоматология", page=2)
        assert len(session.calls) == 2

    def test_cached_response_survives_new_session(self, tmp_path, monkeypatch):
        # первый источник положил состояние в кэш, второй его перечитал
        monkeypatch.setattr(settings, "raw_dir", tmp_path)
        html = wrap_state({"items": [ORG_NODE]})
        first = YandexSource(session=self._FakeSession(html), use_cache=True)
        first.search("стоматология", page=1)

        second = YandexSource(session=self._FakeSession(html), use_cache=True)
        page = second.search("стоматология", page=1)
        assert page.from_cache is True

    def test_cards_parsed_from_page(self):
        session = self._FakeSession(wrap_state({"items": [ORG_NODE]}))
        source = YandexSource(session=session, use_cache=False)
        cards = source.cards(source.search("стоматология"))
        assert len(cards) == 1
        assert cards[0].phones == ["+375296033656", "+3751733333333"]

    def test_bbox_centre_becomes_search_point(self):
        session = self._FakeSession(wrap_state({"items": [ORG_NODE]}))
        source = YandexSource(session=session, use_cache=False)
        source.search("аптека", bbox="28.400000,54.200000~28.500000,54.300000")
        # центр прямоугольника: 28.45, 54.25
        assert "ll=28.450000%2C54.250000" in session.calls[0]

    def test_broken_bbox_falls_back_to_city_centre(self):
        session = self._FakeSession(wrap_state({"items": [ORG_NODE]}))
        source = YandexSource(session=session, use_cache=False)
        source.search("аптека", bbox="мусор")
        expected = f"{settings.city_center_lon:.6f}%2C{settings.city_center_lat:.6f}"
        assert expected in session.calls[0]

    def test_cache_can_be_switched_off(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "raw_dir", tmp_path)
        session = self._FakeSession(wrap_state({"items": [ORG_NODE]}))
        source = YandexSource(session=session, use_cache=False)
        source.search("аптека", page=1)
        source.search("аптека", page=1)
        assert len(session.calls) == 2


class TestConstants:
    def test_pages_per_query_sane(self):
        assert 1 <= yandex_ssr.PAGES_PER_QUERY <= 5

    def test_source_name(self):
        assert YandexSource.name


def test_settings_do_not_blow_up_with_real_env():
    """Настройки из .env должны оставаться валидными."""
    assert isinstance(Settings(), Settings)
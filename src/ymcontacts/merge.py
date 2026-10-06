"""Склейка филиалов одной организации.

В картах одна организация часто имеет несколько карточек: «Суши Wok» на улице
Ленина и «Суши Wok» на Победы — это две карточки с разными адресами и своими
телефонами, но одна компания. Для рассылки нужна одна строка со всеми
контактами, поэтому карточки склеиваются по ключу из normalize.org_key().

Важно понимать границу склейки: склеиваются только карточки с одинаковым
ключом названия. Разные организации с похожими названиями в разных
районах склеиваются только если ключ совпал — это осознанный компромисс
между «лишняя склейка» и «разорванная база».
"""

from __future__ import annotations

from .logging_setup import get_logger
from .models import MergedOrg

log = get_logger(__name__)


def _unique(values: list[str]) -> list[str]:
    """Убрать пустые и повторы, сохранив порядок (порядок = приоритет источника)."""
    return list(dict.fromkeys(v for v in values if v))


def _merge_dicts(target: dict[str, list[str]], additions: dict[str, str]) -> None:
    """Дописать значения в накопительный словарь соцсетей/мессенджеров."""
    for key, value in additions.items():
        if not value:
            continue
        bucket = target.setdefault(key, [])
        if value not in bucket:
            bucket.append(value)


def merge_cards(rows: list[dict]) -> list[MergedOrg]:
    """Склеить карточки (словари из storage.orgs_with_contacts) в сводные записи."""
    groups: dict[str, list[dict]] = {}

    for row in rows:
        key = row.get("group_key") or row.get("name", "").lower()
        groups.setdefault(key, []).append(row)

    merged: list[MergedOrg] = []

    for _key, cards in groups.items():
        # Сортируем так, чтобы карточка с наибольшим числом контактов была первой:
        # её телефон и сайт становятся "основными" в CSV.
        def contact_score(card: dict) -> int:
            return len(card.get("phones") or []) + (1 if card.get("website") else 0)

        cards_sorted = sorted(cards, key=contact_score, reverse=True)

        org = MergedOrg(
            name=cards_sorted[0]["name"],
            branches=len(cards),
            last_seen=max((c.get("last_seen") or "") for c in cards),
        )

        for card in cards_sorted:
            org.categories.extend(card.get("categories") or [])
            if card.get("address"):
                org.addresses.append(card["address"])
            org.phones.extend(card.get("phones") or [])
            org.emails.extend(card.get("emails") or [])
            if card.get("website"):
                org.websites.append(card["website"])
            _merge_dicts(org.socials, card.get("socials") or {})
            _merge_dicts(org.messengers, card.get("messengers") or {})

            lat, lon = card.get("lat"), card.get("lon")
            if lat is not None and lon is not None:
                org.coords.append(f"{lat:.6f},{lon:.6f}")

            if card.get("url"):
                org.source_urls.append(card["url"])

        # Финальная чистка дублей
        org.categories = _unique(org.categories)
        org.addresses = _unique(org.addresses)
        org.phones = _unique(org.phones)
        org.emails = _unique(org.emails)
        org.websites = _unique(org.websites)
        org.coords = _unique(org.coords)
        org.source_urls = _unique(org.source_urls)
        org.socials = {k: _unique(v) for k, v in org.socials.items()}
        org.messengers = {k: _unique(v) for k, v in org.messengers.items()}

        # Флаги для удобной фильтрации в Excel
        org.with_phone = bool(org.phones)
        org.with_email = bool(org.emails)

        merged.append(org)

    log.info("склеено карточек: %s -> организаций: %s", len(rows), len(merged))
    return merged


def has_any_contact(org: MergedOrg) -> bool:
    """Есть ли хоть один канал связи — телефон, почта или мессенджер."""
    return bool(org.phones or org.emails or org.messengers or org.socials)
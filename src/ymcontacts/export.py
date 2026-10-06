"""Выгрузка в CSV.

Два решения, о которых стоит сказать:

  * разделитель — точка с запятой. В русском Excel это то, что нужно,
    чтобы файл открылся таблицей, а не одной колонкой;
  * кодировка UTF-8 с BOM. Без BOM русские буквы в Excel превращаются
    в «Ð�Ñ€Ð°Ð²Ð¸Ð»Ð».
"""

from __future__ import annotations

import csv
from pathlib import Path

from .logging_setup import get_logger
from .models import MergedOrg

log = get_logger(__name__)

# Заголовки в человекочитаемом виде — так удобнее работать в Excel
COLUMNS = [
    ("Название", lambda o: o.name),
    ("Рубрики", lambda o: ", ".join(o.categories)),
    ("Адреса", lambda o: " | ".join(o.addresses)),
    ("Телефоны", lambda o: ", ".join(o.phones)),
    ("Email", lambda o: ", ".join(o.emails)),
    ("Сайты", lambda o: ", ".join(o.websites)),
    ("Соцсети", lambda o: _flatten(o.socials)),
    ("Мессенджеры", lambda o: _flatten(o.messengers)),
    ("Координаты", lambda o: " | ".join(o.coords)),
    ("Филиалов", lambda o: o.branches),
    ("Есть телефон", lambda o: "да" if o.with_phone else ""),
    ("Есть email", lambda o: "да" if o.with_email else ""),
    ("Ссылка на карту", lambda o: " | ".join(o.source_urls)),
    ("Обновлено", lambda o: o.last_seen),
]


def _flatten(bucket: dict[str, list[str]]) -> str:
    """{"vk": ["a", "b"]} -> "vk: a, b; ok: c"."""
    parts: list[str] = []
    for name, links in sorted(bucket.items()):
        parts.append(f"{name}: {', '.join(links)}")
    return "; ".join(parts)


def export_csv(orgs: list[MergedOrg], path: Path, only_with_contacts: bool = False) -> Path:
    """Записать сводные организации в CSV. Возвращает путь к файлу."""
    path.parent.mkdir(parents=True, exist_ok=True)

    rows = orgs
    if only_with_contacts:
        rows = [o for o in orgs if o.with_phone or o.with_email or o.messengers or o.socials]

    # utf-8-sig — BOM, чтобы Excel корректно открыл кириллицу
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow([title for title, _ in COLUMNS])
        for org in rows:
            writer.writerow([getter(org) for _, getter in COLUMNS])

    log.info("выгружено строк: %s -> %s", len(rows), path)
    return path


def export_summary(orgs: list[MergedOrg]) -> dict:
    """Сводка по качеству данных — полезно понять, что реально собралось."""
    total = len(orgs)
    with_phone = sum(1 for o in orgs if o.with_phone)
    with_email = sum(1 for o in orgs if o.with_email)
    with_messenger = sum(1 for o in orgs if o.messengers)
    with_site = sum(1 for o in orgs if o.websites)
    return {
        "организаций": total,
        "с телефоном": with_phone,
        "с почтой": with_email,
        "с мессенджером": with_messenger,
        "с сайтом": with_site,
        "% с телефоном": round(with_phone / total * 100, 1) if total else 0,
        "% с почтой": round(with_email / total * 100, 1) if total else 0,
    }
"""Кэш сырых ответов.

Зачем: парсер — штука хрупкая. Если завтра Яндекс поменяет схему ответа,
всё, что мы уже скачали, должно остаться пригодным. Поэтому каждый ответ
сразу пишется на диск в gzip, а разбор идёт поверх кэша.

Побочная польза: разбор можно перепрогнать сколько угодно раз, не трогая сеть.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

log = get_logger(__name__)


def make_cache_key(*parts: Any) -> str:
    """Детерминированный ключ кэша: одинаковые параметры — одинаковый ключ."""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def save_payload(payload: Any, cache_key: str, raw_dir: Path) -> int:
    """Записать ответ в gzip-файл. Возвращает размер файла в байтах."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{cache_key}.json.gz"
    # Пишем во временный файл и переименовываем: так в кэше не появится
    # наполовину записанный файл, даже если процесс убьют посреди записи.
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    tmp.replace(path)
    return path.stat().st_size


def load_payload(cache_key: str, raw_dir: Path) -> Any | None:
    """Прочитать ответ из кэша. None — записи нет или она битая."""
    path = raw_dir / f"{cache_key}.json.gz"
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError, EOFError) as exc:
        # Битый файл не должен ронять прогон — просто идём в сеть.
        log.warning("кэш %s не читается (%s), будет новый запрос", cache_key, exc)
        return None


def clear_cache(raw_dir: Path) -> int:
    """Очистить кэш (например, если источник изменился). Возвращает число файлов."""
    files = list(raw_dir.glob("*.json.gz"))
    for file in files:
        file.unlink(missing_ok=True)
    return len(files)
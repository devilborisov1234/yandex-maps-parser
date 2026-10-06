"""Структуры данных проекта.

Используем обычные dataclass: структуры простые, Pydantic здесь дал бы
лишнюю зависимость без выигрыша.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum


class TaskStatus(StrEnum):
    """Статусы задания в очереди сбора."""

    PENDING = "pending"  # ждёт выполнения
    RUNNING = "running"  # взято в работу (при падении вернётся в retry при старте)
    DONE = "done"  # успешно обработано
    RETRY = "retry"  # временная ошибка, будет повтор
    COOLDOWN = "cooldown"  # источник заблокирован, ждёт таймера
    FAILED = "failed"  # ошибка повторяется, дальше нет смысла tries


class TaskKind(StrEnum):
    """Типы заданий."""

    GENERIC = "generic"  # общий запрос по ячейке без рубрики
    RUBRIC = "rubric"  # запрос по конкретной рубрике внутри ячейки
    SITE = "site"  # этап 2: обход сайта организации за почтой


@dataclass
class SearchPage:
    """Сырой ответ источника по одному запросу."""

    text: str  # поисковый запрос
    bbox: str | None  # прямоугольник поиска
    page: int  # номер страницы выдачи
    payload: dict  # сырой JSON как пришёл (для разбора и отладки)
    from_cache: bool = False  # True — взят из кэша, сеть не трогали


@dataclass
class OrgCard:
    """Карточка организации — одна сущность Яндекса.

    Одна организация может иметь несколько карточек (филиалы), у каждой свой
    адрес и свои координаты. Склейка филиалов делается позже, в merge.py.
    """

    source: str  # "yandex"
    external_id: str  # идентификатор карточки в источнике (uri)
    name: str
    category: str = ""  # основная рубрика
    categories: list[str] = field(default_factory=list)  # все рубрики
    address: str = ""
    phones: list[str] = field(default_factory=list)  # в формате E.164
    website: str = ""
    socials: dict[str, str] = field(default_factory=dict)  # {"vk": "...", "telegram": "..."}
    emails: list[str] = field(default_factory=list)  # заполняется на этапе сайтов
    messengers: dict[str, str] = field(default_factory=dict)  # {"whatsapp": "...", "telegram": "..."}
    lat: float | None = None
    lon: float | None = None
    hours: str = ""
    rating: float | None = None
    reviews_count: int | None = None
    url: str = ""  # ссылка на карточку
    group_key: str = ""  # ключ склейки филиалов (см. normalize.org_key)
    first_seen: str = ""  # когда впервые увидели (ISO)
    last_seen: str = ""  # когда видели в последний раз (ISO)

    def to_row(self) -> dict:
        """Подготовить значения для записи в SQLite (списки — в JSON)."""
        return asdict(self)


@dataclass
class MergedOrg:
    """Сводная запись организации после склейки филиалов — то, что попадает в CSV."""

    name: str
    categories: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    websites: list[str] = field(default_factory=list)
    socials: dict[str, list[str]] = field(default_factory=dict)  # {"vk": ["...", "..."]}
    messengers: dict[str, list[str]] = field(default_factory=dict)
    coords: list[str] = field(default_factory=list)  # ["54.227700,28.485000", ...]
    branches: int = 1
    source_urls: list[str] = field(default_factory=list)
    with_phone: bool = False
    with_email: bool = False
    last_seen: str = ""


@dataclass
class Task:
    """Задание в очереди: одна ячейка × один запрос, либо один сайт на обход."""

    id: int
    kind: TaskKind
    source: str
    dedup_key: str  # уникальный ключ, защищает от дублей заданий
    payload: dict  # параметры задания (text, bbox, page, external_id, ...)
    status: TaskStatus
    attempts: int = 0
    last_error: str | None = None
    cooldown_until: str | None = None


@dataclass
class RunStats:
    """Итоги одного прогона — попадают в отчёт и в таблицу runs."""

    started_at: str = ""
    finished_at: str = ""
    requests_made: int = 0
    requests_from_cache: int = 0
    orgs_new: int = 0
    orgs_updated: int = 0
    tasks_done: int = 0
    tasks_failed: int = 0
    tasks_skipped: int = 0  # закрыто как заведомо пустые страницы выдачи
    captchas: int = 0
    emails_found: int = 0
    sites_processed: int = 0
    notes: list[str] = field(default_factory=list)
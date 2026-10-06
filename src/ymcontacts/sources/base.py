"""Интерфейс источника данных.

Один метод — search(). Всё, что источник умеет отдавать, это JSON-выдача поиска.
Разбор выдачи в карточки — тоже забота адаптера, чтобы раннер не знал,
как устроен конкретный источник.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import OrgCard, SearchPage


class Source(ABC):
    """Базовый класс источника организаций."""

    name: str = "base"

    @abstractmethod
    def search(self, text: str, bbox: str | None = None, page: int = 1) -> SearchPage:
        """Выполнить один поисковый запрос.

        text    — поисковый запрос ("стоматология", название города);
        bbox    — прямоугольник поиска "lon1,lat1~lon2,lat2" либо None;
        page    — номер страницы выдачи (если источник поддерживает пагинацию).

        Возвращает SearchPage с сырым JSON. Исключения классов FetchError
        (CaptchaError / TransientError / SchemaError) обрабатывает раннер.
        """

    def cards(self, page: SearchPage) -> list[OrgCard]:
        """Разобрать сырой ответ в карточки организаций."""
        raise NotImplementedError

    def close(self) -> None:  # noqa: B027 — не все источники требуют закрытия
        """Освободить ресурсы (соединения, браузер). По умолчанию ничего делать не надо."""
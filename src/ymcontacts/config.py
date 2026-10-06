"""Настройки проекта.

Все параметры вынесены в .env (шаблон — .env.example), чтобы значения не были
зашиты в код. Здесь только структура и значения по умолчанию.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Корень проекта: src/ymcontacts/config.py -> src/ymcontacts -> src -> корень
BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Конфигурация сбора. Переменные читаются из .env с префиксом YMC_."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_prefix="YMC_",
        extra="ignore",
    )

    # ---------------------------------------------------------------- город
    city_name: str = "Борисов"
    city_center_lat: float = 54.2277
    city_center_lon: float = 28.4850
    # Радиус обхода в км вокруг центра и размер одной ячейки сетки в км.
    # Для Борисова радиус 7 км покрывает городскую застройку целиком.
    city_radius_km: float = 7.0
    cell_size_km: float = 2.2
    # Ячейка для запросов по рубрикам делается крупнее намеренно: общий запрос
    # по мелкой ячейке нужен для плотного покрытия, а запрос по конкретной
    # рубрике и так сужает выдачу, и мелкая ячейка тут только множит число
    # запросов (110 рубрик × 77 ячеек — это 15 часов вместо 4).
    rubric_cell_size_km: float = 4.4
    # Локаль Яндекса. Для Беларуси — ru_BY.
    lang: str = "ru_BY"
    # Локаль браузера. ВАЖНО: это разные вещи. В параметрах API Яндекс ждёт
    # ru_BY (с подчёркиванием), а браузеру Playwright нужен валидный BCP-47 код
    # ru-BY (с дефисом). С подчёркиванием Chrome грузит страницу карт, но
    # теряет семь JS-модулей приложения — приложение не стартует и не делает
    # ни одного запроса к API. Намёк: Playwright такой код не проверяет.
    browser_locale: str = "ru-BY"
    # Домен карт. Для Беларуси карты живут на yandex.by, для РФ — на yandex.ru.
    maps_host: str = "https://yandex.by"
    # Сегмент города в адресе страницы поиска: /maps/157/<сегмент>/search/...
    # Обязателен: без него Яндекс отвечает 404. Подойдёт любой настоящий
    # город — поиск всё равно идёт по координатам ll. Пусто = взять
    # транслитерацию city_name (Борисов -> borisov).
    maps_city_segment: str = ""

    # ----------------------------------------------------------------- темп
    # Базовая пауза между запросами к Яндексу, секунды (случайное значение в диапазоне).
    delay_min: float = 4.0
    delay_max: float = 9.0
    # Каждые break_every запросов — длинная передышка (имитация перечитывания выдачи).
    break_every: int = 30
    break_min: float = 45.0
    break_max: float = 120.0
    # Потолок запросов в час: страховка от слишком агрессивного темпа.
    max_requests_per_hour: int = 420
    # Сколько ждать после капчи / 429, минут.
    cooldown_on_captcha_min: int = 45

    # -------------------------------------------------------------- браузер
    # "chrome" — использовать установленный Google Chrome (выглядит естественнее
    # бандла Playwright). Пустая строка — бандл chromium из ms-playwright.
    browser_channel: str = "chrome"
    headless: bool = True
    # Размер окна: у Яндекса мобильная вёрстка включается по ширине окна.
    browser_width: int = 1440
    browser_height: int = 900

    # ------------------------------------------------------- этап 2: сайты
    # Сколько сайтов обрабатывать одновременно. Осторожно: больше 4 — уже заметно.
    site_workers: int = 3
    site_delay_min: float = 2.0
    site_delay_max: float = 5.0
    site_timeout: float = 20.0
    # Сколько страниц заходить на сайт: главная + страницы контактов.
    max_pages_per_site: int = 3

    # ---------------------------------------------------------------- пути
    db_path: Path = BASE_DIR / "data" / "collector.db"
    raw_dir: Path = BASE_DIR / "data" / "raw"
    out_dir: Path = BASE_DIR / "data" / "out"
    log_dir: Path = BASE_DIR / "data" / "logs"

    log_level: str = "INFO"

    def ensure_dirs(self) -> None:
        """Создать все рабочие каталоги, если их ещё нет."""
        for path in (self.raw_dir, self.out_dir, self.log_dir, self.db_path.parent):
            path.mkdir(parents=True, exist_ok=True)


# Общий экземпляр настроек. Импортируется по всему проекту: from .config import settings
settings = Settings()
"""Яндекс Карты: браузерная проверка (Playwright).

Сбор данных идёт не здесь, а в yandex_ssr.py: страница поиска отдаётся сервером
уже с готовыми организациями, поэтому для сбора браузер не нужен.

Браузер остался для двух вещей:

  1) ДИАГНОСТИКА. Команда `check` открывает карты и показывает, грузится ли
     приложение, есть ли на странице организации и не показана ли проверка.
     Если сбор перестал работать, смотрят сюда.
  2) РАЗВЕДКА, если серверный рендеринг изменится: открыть страницу глазами
     и посмотреть, что именно сломалось.

Отсюда следует важное: этот модуль не влияет на работоспособность сбора.
Если Playwright недоступен, `sweep` всё равно работает.
"""

from __future__ import annotations

import time
from typing import Any

from ..config import settings
from ..fetcher import USER_AGENT, CaptchaError
from ..logging_setup import get_logger

log = get_logger(__name__)

# Пометка, что страницей управляет автоматизация. Без неё карты меняют
# поведение: считают сессию подозрительной и показывают проверку.
INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
"""

# Признаки того, что браузер упёрся в проверку.
BLOCK_MARKERS = ("smartcaptcha", "checkcaptcha", "Подтвердите, что запросы")


class YandexBrowserChecker:
    """Открывает страницу карт в браузере и проверяет, что всё живо."""

    def __init__(
        self,
        *,
        headless: bool | None = None,
        channel: str | None = None,
        maps_host: str | None = None,
    ) -> None:
        self.headless = settings.headless if headless is None else headless
        self.channel = settings.browser_channel if channel is None else channel
        self.maps_host = maps_host or settings.maps_host

        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    # ------------------------------------------------------------------ запуск
    def start(self) -> None:
        """Открыть браузер."""
        if self._page is not None:
            return

        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()

        # Канал "chrome" — установленный у пользователя Google Chrome.
        launch_args: dict[str, Any] = {"headless": self.headless}
        if self.channel:
            launch_args["channel"] = self.channel

        self._browser = self._playwright.chromium.launch(**launch_args)
        self._context = self._browser.new_context(
            user_agent=USER_AGENT,
            # Локаль строго в формате BCP-47 (ru-BY, с дефисом). С подчёркиванием
            # Chrome стабильно теряет JS-модули карт и приложение не стартует —
            # см. подробный комментарий в config.py.
            locale=settings.browser_locale.replace("_", "-"),
            timezone_id="Europe/Minsk",
            viewport={"width": settings.browser_width, "height": settings.browser_height},
        )
        self._context.add_init_script(INIT_SCRIPT)
        self._context.set_default_timeout(45_000)
        self._page = self._context.new_page()
        log.info("браузер запущен (headless=%s, канал=%s)", self.headless, self.channel or "bundled")

    def close(self) -> None:
        """Закрыть браузер и все его ресурсы."""
        for closer in (
            getattr(self._context, "close", None),
            getattr(self._browser, "close", None),
            getattr(self._playwright, "stop", None),
        ):
            if callable(closer):
                try:
                    closer()
                except Exception as exc:  # закрытие не должно ронять проверку
                    log.debug("ошибка при закрытии браузера: %s", exc)
        self._page = self._context = self._browser = self._playwright = None

    def __enter__(self) -> YandexBrowserChecker:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ----------------------------------------------------------------- проверка
    def _search_url(self, text: str) -> str:
        """Адрес страницы поиска в понятном для человека виде."""
        from urllib.parse import quote, urlencode

        params = urlencode(
            {
                "ll": f"{settings.city_center_lon:.6f},{settings.city_center_lat:.6f}",
                "z": "15",
            }
        )
        return f"{self.maps_host}/maps/157/search/{quote(text, safe='')}/?{params}"

    def check(self, text: str = "аптека", wait: float = 8.0) -> dict:
        """Открыть страницу и вернуть картину: что загрузилось и что сломано.

        Возвращает словарь, который печатает команда `check`.
        """
        from .yandex_ssr import STATE_RE

        self.start()
        counters = {"chunks_ok": 0, "chunks_fail": 0}

        def on_response(response) -> None:
            # JS-модули приложения лежат на maps.yastatic.net
            if "maps.yastatic.net" in response.url and response.status == 200:
                counters["chunks_ok"] += 1

        def on_failed(request) -> None:
            if "yastatic" in request.url:
                counters["chunks_fail"] += 1

        self._page.on("response", on_response)
        self._page.on("requestfailed", on_failed)

        log.info("открываю страницу карт (до 30 с)...")
        self._page.goto(self._search_url(text), wait_until="domcontentloaded", timeout=30_000)

        log.info("жду загрузку приложения (%.0f с)...", wait)
        time.sleep(wait)

        try:
            self._check_blocked()
            blocked = False
        except CaptchaError:
            blocked = True

        html = self._page.content()
        orgs = 0
        match = STATE_RE.search(html)
        if match:
            orgs = match.group(1).count('"shortTitle"')

        return {
            "страница": self._page.url,
            "заголовок": self._page.title(),
            "проверка (капча)": blocked,
            "js-модулей загружено": counters["chunks_ok"],
            "js-модулей не загружено": counters["chunks_fail"],
            "организаций на странице": orgs,
            "размер HTML": len(html),
        }

    def _check_blocked(self) -> None:
        """Проверить страницу на капчу."""
        try:
            content = self._page.content()
        except Exception:
            return
        lowered = content[:30_000].lower()
        if any(marker.lower() in lowered for marker in BLOCK_MARKERS):
            raise CaptchaError("страница карт показала проверку")

    def dump_html(self, path: str) -> None:
        """Сохранить HTML страницы — чтобы посмотреть, что именно изменилось."""
        self.start()
        with open(path, "w", encoding="utf-8") as file:
            file.write(self._page.content())
        log.info("HTML сохранён: %s", path)
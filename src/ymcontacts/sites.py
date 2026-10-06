"""Этап 2: обход сайтов организаций за почтой и мессенджерами.

Зачем этот этап: в Яндекс Картах email нет вообще — ни в карточке, ни в
ответе API. Почта лежит только на сайтах самих организаций. Раз то,
зачем мы всё это собираем, — рассылка, этап обязателен.

Правила вежливости (они же — причина не попасть в блэклист):
  * один и тот же домен не опрашиваем чаще, чем раз в N секунд;
  * проверяем robots.txt и не лезем в запрещённые разделы;
  * ограничиваем глубину: главная + страницы контактов, без обхода всего сайта.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from .fetcher import HttpSession
from .logging_setup import get_logger
from .normalize import clean_url, domain_of, extract_emails, extract_messengers

log = get_logger(__name__)

# Ссылки, за которыми обычно живут контакты
_CONTACT_HINTS = (
    "contact", "contacts", "контакт", "контакты",
    "about", "about-us", "aboutus", "o-nas", "o-nas-kompanii",
    "company", "kontakty", "svyaz", "связь", "о компании",
)

# Телеграм-ссылка внутри mailto-подобного мусора не нужна — уже отфильтровано
_CHARSET_NOTE = "регулярки живут в normalize.py — здесь только обход"


@dataclass
class SiteResult:
    """Что удалось вытащить с одного сайта."""

    url: str
    external_id: str = ""
    emails: list[str] = field(default_factory=list)
    messengers: dict[str, str] = field(default_factory=dict)
    pages_visited: int = 0
    skipped: str | None = None  # причина пропуска (robots.txt и т.п.)
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Есть ли хоть какая-то польза."""
        return bool(self.emails or self.messengers)


# Кэш robots.txt по домену: один сайт проверяем один раз за прогон
_ROBOTS_CACHE: dict[str, RobotFileParser | None] = {}

# Последний момент обращения к домену — для паузы между страницами одного сайта
_LAST_HIT: dict[str, float] = {}


class SiteCrawler:
    """Достаёт почту и мессенджеры с сайта организации."""

    def __init__(
        self,
        *,
        max_pages: int = 3,
        respect_robots: bool = True,
        pause_between_pages: tuple[float, float] = (1.5, 3.5),
        timeout: float = 20.0,
    ) -> None:
        self.max_pages = max_pages
        self.respect_robots = respect_robots
        self.pause_between_pages = pause_between_pages
        self.timeout = timeout

    # --------------------------------------------------------------- robots.txt
    def _robots_allows(self, session: HttpSession, url: str) -> bool:
        """Проверить robots.txt. Отдельный таймаут не роняет весь обход."""
        if not self.respect_robots:
            return True
        host = urlparse(url)
        origin = f"{host.scheme}://{host.netloc}"

        if origin not in _ROBOTS_CACHE:
            parser: RobotFileParser | None = RobotFileParser()
            parser.set_url(f"{origin}/robots.txt")
            try:
                # Короткий таймаут и отдельный вызов: недоступный robots.txt
                # не должен ронять весь обход сайта
                response = session.session.get(f"{origin}/robots.txt", timeout=5.0)
                if response.status_code == 200:
                    parser.parse(response.text.splitlines())
                else:
                    # Нет robots.txt — формально всё разрешено
                    _ROBOTS_CACHE[origin] = None
                    return True
            except Exception as exc:
                log.debug("robots.txt %s недоступен (%s), считаем разрешённым", origin, exc)
                _ROBOTS_CACHE[origin] = None
                return True
            _ROBOTS_CACHE[origin] = parser

        parser = _ROBOTS_CACHE[origin]
        if parser is None:
            return True
        try:
            return parser.can_fetch("*", url)
        except Exception:
            return True

    # ------------------------------------------------------------------ обход
    def crawl(self, url: str, external_id: str = "") -> SiteResult:
        """Обойти сайт и собрать почту и мессенджеры."""
        result = SiteResult(url=url, external_id=external_id)
        url = clean_url(url)
        if not url:
            result.skipped = "некорректный адрес"
            return result

        domain = domain_of(url)
        with HttpSession(timeout=self.timeout, language="ru,ru-BY;q=0.9") as session:
            if not self._robots_allows(session, url):
                log.info("robots.txt запрещает %s — пропускаю", url)
                result.skipped = "запрещено robots.txt"
                return result

            try:
                html = session.get_text(url)
            except Exception as exc:
                result.error = str(exc)[:200]
                log.debug("сайт %s недоступен: %s", domain, result.error)
                return result

            result.pages_visited = 1
            self._absorb(result, html, url)

            # Ищем страницы контактов и идём на них, пока не исчерпан лимит
            for next_url in self._contact_pages(html, url)[: self.max_pages - 1]:
                self._wait_turn(domain)
                if not self._robots_allows(session, next_url):
                    continue
                try:
                    page_html = session.get_text(next_url)
                except Exception as exc:
                    log.debug("страница %s недоступна: %s", next_url, exc)
                    continue
                result.pages_visited += 1
                self._absorb(result, page_html, next_url)

        # Почты без дубликатов, порядок сохраняем
        result.emails = list(dict.fromkeys(result.emails))
        log.info(
            "сайт %s: страниц %s, почт %s, мессенджеров %s",
            domain, result.pages_visited, len(result.emails), len(result.messengers),
        )
        return result

    def _absorb(self, result: SiteResult, html: str, page_url: str) -> None:
        """Достать из HTML почты и мессенджеры и добавить в результат."""
        for email in extract_emails(html):
            if email not in result.emails:
                result.emails.append(email)
        for name, link in extract_messengers(html).items():
            result.messengers.setdefault(name, link)

    def _contact_pages(self, html: str, base_url: str) -> list[str]:
        """Найти на странице ссылки, похожие на страницы контактов.

        Полноценный парсинг ссылок делаем дешевле: ищем вхождения подсказок
        в URL. Этого хватает, чтобы попасть в "Контакты" вместо обхода
        всего сайта по спинке ссылок.
        """
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "lxml")
            anchors = soup.find_all("a", href=True)
        except Exception as exc:
            log.debug("не удалось разобрать HTML (%s), ссылки пропускаем", exc)
            return []

        base_domain = domain_of(base_url)
        found: list[str] = []
        for anchor in anchors[:200]:
            href = anchor["href"]
            if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            absolute = urljoin(base_url, href)
            if domain_of(absolute) != base_domain:
                continue  # чужие домены не трогаем
            lowered = absolute.lower()
            if any(hint in lowered for hint in _CONTACT_HINTS):
                clean = clean_url(absolute)
                if clean and clean not in found:
                    found.append(clean)
        return found

    def _wait_turn(self, domain: str) -> None:
        """Пауза между страницами одного домена."""
        last = _LAST_HIT.get(domain, 0.0)
        pause = random.uniform(*self.pause_between_pages)
        remaining = pause - (time.monotonic() - last)
        if remaining > 0:
            time.sleep(remaining)
        _LAST_HIT[domain] = time.monotonic()
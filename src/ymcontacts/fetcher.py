"""HTTP-доставка: сессия, ретраи и классификация ошибок.

Ключевое решение: клиент — curl_cffi с режимом impersonate="chrome".
Причина конкретная: обычный httpx (и его ssl) отдаёт TLS-отпечаток, который
Яндекс опознаёт как бот — отвечает 403 с капчей на первом же запросе.
curl_cffi повторяет отпечаток настоящего Chrome (TLS, HTTP/2, порядок заголовков),
поэтому запрос проходит без маскировки и без браузера.

Ключевая мысль модуля: ретраи бывают разные, и относятся они к разным классам.
Смешивать их в один except — главный источник бана, потому что на капчу
"повторить с backoff" делает ровно то, чего ждёт Яндекс.

  TransientError  — таймаут, обрыв, 5xx. Это сетевой шум, повтор уместен.
  CaptchaError    — 429/403 или маркеры капчи. Повтор немедленно вреден,
                    нужен длинный остывающий перерыв.
  SchemaError     — ответ пришёл, но структура незнакомая. Повтор не поможет,
                    надо разбираться. Данные в базу не пишем.
  NotFoundError   — 404, объект удалён. Помечаем задание проваленным.
"""

from __future__ import annotations

import random
import time
from typing import Any

from curl_cffi import requests as curl_requests
from curl_cffi.requests.errors import RequestsError

from .logging_setup import get_logger

log = get_logger(__name__)

# Актуальный User-Agent Chrome. Совпадение с настоящим браузером важно:
# подделанный UA при настоящих Client Hints — сам по себе признак бота.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

# Кого имитируем. Chrome актуален и по TLS, и по порядку заголовков.
IMPERSONATE = "chrome"

# Признаки того, что нас поймали: капча или промежуточная заглушка.
CAPTCHA_MARKERS = (
    "smartcaptcha",
    "checkcaptcha",
    "captcha-image",
    "showcaptcha",
    "captcha_required",
    "Подтвердите, что запросы отправляли вы",
)

# Признаки страницы-заглушки (JS-челлендж).
BLOCK_MARKERS = (
    "yandexcaptcha",
    "are you not a robot",
    "checking your browser",
)


class FetchError(Exception):
    """Базовая ошибка доставки."""

    retryable: bool = True


class TransientError(FetchError):
    """Сетевой шум: таймаут, обрыв, 5xx. Повторять уместно."""


class CaptchaError(FetchError):
    """Капча или бан. Повтор немедленно только вредит."""

    retryable = False


class SchemaError(FetchError):
    """Ответ получен, но структура неизвестна. Нужен разбор, а не повтор."""

    retryable = False


class NotFoundError(FetchError):
    """Объект не найден — помечаем задание проваленным, чтобы не крутить вечно."""

    retryable = False


def looks_like_captcha(status_code: int, body: str) -> bool:
    """Понять, что это капча или блокировка."""
    if status_code in (403, 429):
        return True
    lowered = body[:5000].lower()
    return any(marker in lowered for marker in CAPTCHA_MARKERS + BLOCK_MARKERS)


class HttpSession:
    """HTTP-клиент с постоянной сессией и ретраями только на сетевом шуме.

    Постоянная сессия важна не только ради кук: так сохраняется и набор
    заголовков, который накопил сервер, — новая "личность" на каждый запрос
    выглядит аномально.
    """

    def __init__(
        self,
        *,
        timeout: float = 25.0,
        user_agent: str = USER_AGENT,
        language: str = "ru-BY,ru;q=0.9,en;q=0.8",
        max_retries: int = 3,
        referer: str | None = None,
        impersonate: str | None = IMPERSONATE,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = curl_requests.Session(impersonate=impersonate or "chrome")
        # Заголовки по умолчанию применяем ко всем запросам сессии
        self.default_headers = {
            "User-Agent": user_agent,
            "Accept-Language": language,
        }
        if referer:
            self.default_headers["Referer"] = referer
        self.session.headers.update(self.default_headers)

    def close(self) -> None:
        """Закрыть соединения."""
        try:
            self.session.close()
        except Exception as exc:  # закрытие не должно ронять прогон
            log.debug("ошибка при закрытии сессии: %s", exc)

    def __enter__(self) -> HttpSession:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------------------------------------------------------------- запросы
    def get(self, url: str, params: dict | None = None, headers: dict | None = None) -> Any:
        """GET с ретраями только на сетевом шуме.

        Капча наружу сразу: повторять её — значит долбить заблокированный адрес.
        """
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(
                    url, params=params, headers=headers, timeout=self.timeout
                )
            except RequestsError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                # Экспоненциальная пауза с джиттером: 2с, 4с, 8с ± 40%
                pause = (2**attempt) * random.uniform(0.6, 1.4)
                log.warning("сетевой сбой (%s), повтор %s через %.1f с", exc, attempt, pause)
                time.sleep(pause)
                continue

            status = response.status_code
            body = response.text or ""

            if looks_like_captcha(status, body):
                log.error("капча или блокировка (HTTP %s) на %s", status, url)
                raise CaptchaError(f"HTTP {status}")

            if status == 404:
                raise NotFoundError("HTTP 404")
            if status >= 500:
                raise TransientError(f"HTTP {status}")
            if status >= 400:
                # 400/401/418 и прочее — не шум, повтор не поможет
                raise FetchError(f"HTTP {status}")

            return response

        raise TransientError(str(last_error) if last_error else "неизвестная ошибка сети")

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> Any:
        """GET с разбором JSON. Неожиданная структура — отдельный класс SchemaError."""
        response = self.get(url, params=params, headers=headers)
        try:
            return response.json()
        except ValueError as exc:
            preview = (response.text or "")[:300].replace("\n", " ")
            raise SchemaError(f"ответ не JSON: {preview}") from exc

    def get_text(self, url: str, params: dict | None = None, headers: dict | None = None) -> str:
        """GET с ожиданием HTML (страницы карт и сайты организаций)."""
        return self.get(url, params=params, headers=headers).text

    def set_cookies_from_browser(self, cookies: list[dict]) -> None:
        """Пересадить куки из браузера в эту сессию.

        Смысл: браузер один раз открывает страницу, мы снимаем его куки —
        дальше HTTP-запросы идут как "тот же посетитель".
        """
        for cookie in cookies:
            try:
                self.session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain"),
                    path=cookie.get("path", "/"),
                )
            except (KeyError, ValueError) as exc:
                log.debug("кука %s не перенесена: %s", cookie.get("name"), exc)
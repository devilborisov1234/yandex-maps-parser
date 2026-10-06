"""Темп запросов (пайсинг).

Это самый важный модуль с точки зрения надёжности сбора. Задача — не «сходить
как можно быстро», а «сходить так, чтобы не выделяться в потоке».

Модель задержек:
  * базовая пауза — случайное значение из [delay_min, delay_max];
  * длинная передышка каждые break_every запросов (имитация «человек перечитывает выдачу»);
  * ограничение количества запросов в час;
  * уважение внешней блокировки (капча / 429) — пауза задаётся извне.

Важно: джиттер ограниченный, а не равномерный и не полностью случайный.
Равномерные интервалы выглядят механически, полностью случайные — аномально
по энтропии. Нужна смесь: базовая задержка + редкие длинные паузы.
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque

from .logging_setup import get_logger

log = get_logger(__name__)


class StopRequested(Exception):
    """Пользователь нажал Ctrl+C — пора корректно завершиться."""


class Pacer:
    """Считает время до следующего запроса и спит, соблюдая лимиты."""

    def __init__(
        self,
        delay_min: float = 4.0,
        delay_max: float = 9.0,
        break_every: int = 30,
        break_min: float = 45.0,
        break_max: float = 120.0,
        max_per_hour: int = 420,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.break_every = break_every
        self.break_min = break_min
        self.break_max = break_max
        self.max_per_hour = max_per_hour

        self.stop_event = stop_event or threading.Event()
        self._last_request: float | None = None  # когда был последний запрос (monotonic)
        self._since_break = 0  # счётчик запросов с последней передышки
        self._blocked_until: float = 0.0  # до какого момента внешняя блокировка
        self._hour: deque[float] = deque(maxlen=10_000)  # метки времени последних запросов

    # ------------------------------------------------------------ публичные методы
    def wait(self) -> float:
        """Подождать перед следующим запросом. Возвращает фактическую паузу, секунды.

        Бросает StopRequested, если выставлен stop_event (аккуратный выход).
        """
        now = time.monotonic()
        chunks: list[float] = []

        # 1) Если источник заблокирован — спим до конца блокировки.
        if self._blocked_until > now:
            pause = self._blocked_until - now
            log.warning("источник заблокирован, пауза %.0f с (%.1f мин)", pause, pause / 60)
            chunks.append(pause)

        # 2) Пауза до конца текущего часа, если лимит запросов выбран.
        hour_pause = self._hour_pause(now)
        if hour_pause > 0:
            chunks.append(hour_pause)

        # 3) Передышка после серии запросов.
        if self._since_break >= self.break_every:
            pause = random.uniform(self.break_min, self.break_max)
            log.info("передышка после %s запросов: %.0f с", self._since_break, pause)
            self._since_break = 0
            chunks.append(pause)

        # 4) Базовая задержка от последнего запроса.
        if self._last_request is not None:
            elapsed = time.monotonic() - self._last_request
            base = random.uniform(self.delay_min, self.delay_max)
            if elapsed < base:
                chunks.append(base - elapsed)

        total = max(chunks) if chunks else 0.0
        if total > 0:
            self._sleep_interruptible(total)
            if self.stop_event.is_set():
                raise StopRequested

        self._mark_request()
        return total

    def block(self, minutes: float) -> None:
        """Задать внешнюю блокировку источника (после капчи или 429)."""
        self._blocked_until = time.monotonic() + minutes * 60
        log.warning("блокировка источника на %.0f мин", minutes)

    def reset_after_success(self) -> None:
        """Успешный ответ — значит лимиты можно немного ослабить."""
        self._blocked_until = 0.0

    def request(self) -> None:
        """Отметить факт запроса (вызывается после успешного ответа)."""
        self._mark_request()

    def skip_wait(self) -> None:
        """Пропустить ожидание (используется при чтении из кэша)."""
        self._since_break = 0

    # ---------------------------------------------------------------- внутреннее
    def _hour_pause(self, now: float) -> float:
        """Сколько ждать, чтобы не превысить лимит запросов в час."""
        cutoff = now - 3600
        while self._hour and self._hour[0] < cutoff:
            self._hour.popleft()
        if len(self._hour) < self.max_per_hour:
            return 0.0
        # Самый старый запрос выйдет за час через (hour[0] + 3600 - now)
        return max(0.0, self._hour[0] + 3600 - now)

    def _mark_request(self) -> None:
        """Отметить, что запрос только что ушёл."""
        now = time.monotonic()
        self._last_request = now
        self._hour.append(now)
        self._since_break += 1

    def _sleep_interruptible(self, seconds: float) -> None:
        """Спать, но просыпаться каждые 0.5 с, чтобы быстро реагировать на Ctrl+C."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.stop_event.wait(min(0.5, deadline - time.monotonic())):
                raise StopRequested

    def snapshot(self) -> dict:
        """Состояние пайсера для отчёта."""
        return {
            "requests_last_hour": len(self._hour),
            "since_break": self._since_break,
            "blocked_for_sec": max(0.0, self._blocked_until - time.monotonic()),
        }
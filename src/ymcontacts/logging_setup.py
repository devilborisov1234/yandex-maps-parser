"""Логирование.

Пишем одновременно в консоль (чтобы видеть прогресс) и в файл (чтобы разбирать
проблемы после прогона). Файл переезжает на дату, чтобы не рос бесконечно.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from datetime import date

_CONFIGURED = False


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """Настроить корневой логгер. Повторный вызов ничего не делает."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Формат: время | уровень | сообщение
    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file:
        # Ротация: один файл на день, хранить 14 дней
        handler = logging.handlers.TimedRotatingFileHandler(
            filename=f"{log_file}.{date.today():%Y-%m-%d}.log",
            when="midnight",
            backupCount=14,
            encoding="utf-8",
        )
        handler.setFormatter(fmt)
        root.addHandler(handler)

    # Сторонние библиотеки шумят — приглушаем до WARNING
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Логгер для модуля: get_logger(__name__)."""
    return logging.getLogger(name)
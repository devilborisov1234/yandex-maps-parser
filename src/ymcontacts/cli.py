"""Командный интерфейс.

Примеры:
    python -m ymcontacts check               # проверка доступа (первый шаг)
    python -m ymcontacts plan --rubrics core # построить очередь обхода
    python -m ymcontacts sweep --hours 3     # собрать данные (3 часа)
    python -m ymcontacts sites               # добрать почту с сайтов
    python -m ymcontacts export              # выгрузить CSV
    python -m ymcontacts report              # что уже собрано

Ctrl+C в любой момент ставит мягкую остановку: текущий запрос доходит до конца,
очередь и таймеры сохраняются, прогон можно продолжить позже.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from datetime import datetime

from . import rawcache
from .config import settings
from .export import export_csv, export_summary
from .logging_setup import setup_logging
from .merge import merge_cards
from .runner import CORE_RUBRICS, GENERIC_QUERIES, Runner

# Флаг остановки, который общий для всех слоёв
_stop = threading.Event()


def _on_sigint(signum, frame) -> None:  # noqa: ARG001
    """Ctrl+C → немедленное прерывание.

    Раньше первый Ctrl+C только ставил флаг, а ждать его в браузере приходилось
    до минуты: выглядело так, что программа не реагирует, и приходилось жать
    второй раз. Теперь первый нажатие сразу прерывает работу.
    """
    raise KeyboardInterrupt


# ==========================================================================
# Команды
# ==========================================================================
def cmd_check(args: argparse.Namespace) -> int:
    """Проверить, что данные Яндекс Карт доступны, и показать первые организации.

    Это первая команда, которую нужно выполнить: если здесь всё в порядке,
    сбор работает. Ничего не собирает и ничего не меняет.
    """
    from .sources.yandex_ssr import YandexSource

    print("=== 1. Страница поиска карт (ровно так же, как идёт сбор) ===")
    source = YandexSource()
    try:
        result = source.search(text=args.text)
        cards = source.cards(result)
    except Exception as exc:
        print(f"  Не сработало: {type(exc).__name__}: {exc}")
        print('  Смотрите раздел "Если что-то пошло не так" в README.md')
        return 1
    finally:
        source.close()

    with_phone = sum(1 for card in cards if card.phones)
    with_site = sum(1 for card in cards if card.website)
    print(f"  Ответ получен. Организаций: {len(cards)}")
    print(f"  с телефонами: {with_phone}, с сайтом: {with_site}")

    for card in cards[:3]:
        categories = ", ".join(card.categories[:2]) or "-"
        print()
        print(f"  {card.name}")
        print(f"    рубрики:    {categories}")
        print(f"    адрес:      {card.address or '-'}")
        print(f"    телефоны:   {', '.join(card.phones) or '-'}")
        print(f"    сайт:      {card.website or '-'}")
        print(f"    соцсети:    {card.socials or '-'}")
        print(f"    координаты: {card.lat}, {card.lon}")

    if args.browser:
        print()
        print("=== 2. Браузерная проверка (только для диагностики) ===")
        from .sources.yandex_browser import YandexBrowserChecker

        try:
            with YandexBrowserChecker() as checker:
                info = checker.check(text=args.text)
        except Exception as exc:
            print(f"  Браузерная проверка не удалась: {type(exc).__name__}: {exc}")
            print("  На сбор это не влияет: сбор работает без браузера.")
            return 0
        for key, value in info.items():
            print(f"  {key}: {value}")

    print()
    print("Всё работает. Дальше: python -m ymcontacts plan")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Построить очередь заданий: сетка города + запросы по рубрикам."""
    with Runner(stop_event=_stop) as runner:
        cells = len(runner.cells())

        print("=== Оценка объёма ===")
        for key, value in runner.estimate_requests(include_rubrics=args.rubrics == "core").items():
            print(f"  {key}: {value}")

        total = runner.plan_grid()
        queries = " и ".join(f"«{q}»" for q in GENERIC_QUERIES)
        print(
            f"\nОбщих запросов ({len(GENERIC_QUERIES)} шт.: {queries} × страницы × "
            f"мелкая сетка, {cells} ячеек): +{total}"
        )

        if args.rubrics == "core":
            count = runner.plan_rubrics(CORE_RUBRICS)
            print(f"Запросов по рубрикам ({len(CORE_RUBRICS)} рубрик): +{count}")

        print(f"\nВ очереди: {runner.db.queue_stats()}")
        print("\nДальше: python -m ymcontacts sweep")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    """Основной обход: собрать организации из Яндекс Карт."""
    with Runner(stop_event=_stop) as runner:
        stats = runner.sweep(
            max_requests=args.max_requests,
            max_hours=args.hours,
            followups=not args.no_followups,
        )

        print("\n=== Итог обхода ===")
        print(f"Запросов в сеть:      {stats.requests_made}")
        print(f"Из кэша:             {stats.requests_from_cache}")
        print(f"Новых организаций:   {stats.orgs_new}")
        print(f"Обновлено карточек:  {stats.orgs_updated}")
        print(f"Всего в базе:        {runner.db.count_orgs()}")
        print(f"Очередь:             {runner.db.queue_stats()}")
        for note in stats.notes:
            print(f"Важно: {note}")
    return 0


def cmd_sites(args: argparse.Namespace) -> int:
    """Этап 2: обойти сайты и добыть почту и мессенджеры."""
    with Runner(stop_event=_stop) as runner:
        stats = runner.run_sites(limit=args.limit)
        print("\n=== Итог обхода сайтов ===")
        print(f"Сайтов обработано:   {stats.sites_processed}")
        print(f"Почт найдено:       {stats.emails_found}")
        print(f"Организаций с почтой: {runner.db.stats().get('with_email')}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Склеить филиалы и выгрузить CSV."""
    with Runner(stop_event=_stop) as runner:
        rows = runner.db.orgs_with_contacts()
        if not rows:
            print("В базе пока нет организаций. Сначала выполните sweep.")
            return 1

        merged = merge_cards(rows)
        out = args.out or (settings.out_dir / f"{settings.city_name.lower()}_contacts.csv")
        export_csv(merged, out, only_with_contacts=args.only_contacts)

        print(f"\nФайл: {out}")
        print("\n=== Сводка по данным ===")
        for key, value in export_summary(merged).items():
            print(f"  {key}: {value}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Показать состояние базы и очереди."""
    with Runner(stop_event=_stop) as runner:
        report = runner.report()
        print("=== Состояние ===")
        for key, value in report.items():
            print(f"  {key}: {value}")

        last = runner.db.last_run()
        if last:
            print(f"\nПоследний прогон: {last['started_at']} → {last['finished_at'] or 'ещё идёт'}")
    return 0


def cmd_clear_cache(args: argparse.Namespace) -> int:
    """Очистить кэш сырых ответов (после смены логики разбора или шаблона)."""
    removed = rawcache.clear_cache(settings.raw_dir)
    print(f"Удалено файлов кэша: {removed}")
    return 0


# ==========================================================================
# Разбор аргументов
# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    """Собрать описание команд."""
    parser = argparse.ArgumentParser(
        prog="python -m ymcontacts",
        description="Сбор организаций и контактов из Яндекс Карт",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # check
    p = sub.add_parser("check", help="проверить доступ к данным карт (первый шаг)")
    p.add_argument("--text", default="аптека", help="запрос для проверки")
    p.add_argument("--browser", action="store_true", help="дополнительно открыть карты в браузере")
    p.set_defaults(func=cmd_check)

    # plan
    p = sub.add_parser("plan", help="построить очередь обхода")
    p.add_argument(
        "--rubrics", choices=["core", "none"], default="core",
        help="добавлять ли запросы по базовому списку рубрик (по умолчанию core)",
    )
    p.set_defaults(func=cmd_plan)

    # sweep
    p = sub.add_parser("sweep", help="собрать организации из карт")
    p.add_argument("--max-requests", type=int, default=None, help="лимит запросов за запуск")
    p.add_argument("--hours", type=float, default=None, help="лимит времени в часах")
    p.add_argument("--no-followups", action="store_true", help="не добавлять уточнения по рубрикам")
    p.set_defaults(func=cmd_sweep)

    # sites
    p = sub.add_parser("sites", help="обойти сайты за почтой и мессенджерами")
    p.add_argument("--limit", type=int, default=None, help="сколько сайтов обработать за запуск")
    p.set_defaults(func=cmd_sites)

    # export
    p = sub.add_parser("export", help="выгрузить CSV")
    p.add_argument("--out", default=None, help="путь к файлу")
    p.add_argument("--only-contacts", action="store_true", help="только организации с контактами")
    p.set_defaults(func=cmd_export)

    # report
    p = sub.add_parser("report", help="состояние базы и очереди")
    p.set_defaults(func=cmd_report)

    # clear-cache
    p = sub.add_parser("clear-cache", help="очистить кэш сырых ответов")
    p.set_defaults(func=cmd_clear_cache)

    return parser


def _force_utf8_output() -> None:
    """Переключить вывод консоли на UTF-8.

    На Windows в зависимости от кодовой страницы кириллица в консоли
    превращается в «кракозябры». Это особенно мешает, когда вывод команды
    перенаправляется в файл или в консоль IDE.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if stream and encoding not in {"utf8"}:
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass  # поток не поддерживает перенастройку — не страшно


def main(argv: list[str] | None = None) -> int:
    """Точка входа. Возвращает код возврата для sys.exit."""
    _force_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)

    settings.ensure_dirs()
    setup_logging(settings.log_level, str(settings.log_dir / "collector"))

    # Мягкая остановка по Ctrl+C вместо аварийного завершения
    signal.signal(signal.SIGINT, _on_sigint)

    log_start = f"{datetime.now():%Y-%m-%d %H:%M:%S} — команда {args.command}"
    print(f"\n{log_start}")

    try:
        return args.func(args)
    except KeyboardInterrupt:
        # Прерывание — это нормальный исход, а не ошибка: прогресс сохранён
        print("\nПрервано. Прогресс сохранён, можно продолжить той же командой.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
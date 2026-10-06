"""Оркестратор прогона: очередь, темп, повторы, отчёт.

Логика прогона ровно такая, как задумано на старте:

  1. план       — режем город на сетку и кладём в очередь задания;
  2. обход      — берём задания по одному, соблюдая темп, пишем карточки;
  3. дополнение — по рубрикам, которые реально встретились в ячейке;
  4. сайты      — отдельным проходом добываем почту и мессенджеры;
  5. выгрузка   — склейка филиалов и CSV.

Прогон можно прервать в любой момент (Ctrl+C) и продолжить позже: очередь,
счётчики и таймеры блокировок лежат в базе, а не в памяти.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .config import settings
from .fetcher import CaptchaError, NotFoundError, SchemaError, TransientError
from .grid import Cell, build_grid
from .logging_setup import get_logger
from .models import RunStats, TaskKind
from .normalize import org_key
from .pacing import Pacer, StopRequested
from .sites import SiteCrawler
from .sources.yandex_ssr import PAGES_PER_QUERY
from .storage import Database, open_db

log = get_logger(__name__)

# Общие запросы-разведчики: ими проходим сетку, чтобы узнать рубрики ячейки.
# Название города сюда входит намеренно: см. замеры в plan_grid.
GENERIC_QUERIES = ("организации", "компания")

SOURCE = "yandex"

# Базовый список рубрик для обхода. Не перечисляет всё — задача «все рубрики»
# решается двумя механизмами: этим списком плюс автоматическим дополнением
# рубриками, которые реально встретились в ячейке (см. _plan_followups).
CORE_RUBRICS: tuple[str, ...] = (
    "стоматология", "поликлиника", "больница", "аптека", "оптика", "лаборатория",
    "ветеринарная клиника", "зоотовары", "парикмахерская", "салон красоты",
    "студия красоты", "маникюр", "массаж", "солярий", "барбершоп",
    "спортзал", "фитнес клуб", "бассейн", "танцевальная студия", "йога",
    "кафе", "ресторан", "бар", "пиццерия", "суши", "шашлык", "кофейня",
    "пекарня", "кондитерская", "столовая", "суши бар",
    "продукты", "супермаркет", "магазин одежды", "обувной магазин",
    "бытовая техника", "электроника", "мебель", "строительные материалы",
    "сантехника", "инструменты", "электротовары", "отделочные материалы",
    "окна", "натяжные потолки", "двери", "полы", "ламинат",
    "автосервис", "шиномонтаж", "мойка", "автоэлектрик", "кузовной ремонт",
    "автозапчасти", "эвакуатор", "такси", "грузоперевозки", "логистика",
    "банк", "страховая", "почта", "сотовые телефоны", "салон связи",
    "ремонт телефонов", "интернет провайдер", "компьютерный клуб",
    "гостиница", "хостел", "квартиры посуточно", "банный комплекс", "сауна",
    "клининг", "химчистка", "прачечная", "ремонт квартир", "уборка",
    "юридические услуги", "бухгалтерия", "аудит", "налоговый консультант",
    "рекламное агентство", "веб студия", "типография", "полиграфия",
    "фотостудия", "фотограф", "видеосъёмка", "event агентство",
    "репетитор", "курсы", "языковая школа", "детский центр", "кружок",
    "детский сад", "школа", "гимназия", "колледж", "спортивная школа",
    "медсестра на дом", "сиделка", "пансионат", "дом престарелых",
    "боулинг", "бильярд", "караоке", "антиквариат", "ювелирные изделия",
    "ломбард", "ритуальные услуги", "агентство недвижимости", "ипотека",
    "кредиты", "микрозаймы", "обмен валюты", "подбор персонала",
)

# Сколько рубрик из одной ячейки ставить в дополнение (не больше — иначе очередь
# разрастается быстрее, чем мы её успеваем обработать)
MAX_FOLLOWUP_RUBRICS = 10

# Сколько страниц выдачи забирать за один запрос по рубрике.
# Общий запрос забирает PAGES_PER_QUERY (2), запрос по рубрике — столько же:
# в пределах одной точки конкретная рубрика быстро исчерпывается.
RUBRIC_PAGES = 2


class Runner:
    """Прогон сбора данных."""

    def __init__(
        self,
        db: Database | None = None,
        source=None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.settings = settings
        self.stop_event = stop_event or threading.Event()
        self.db = db or open_db()
        self._owns_db = db is None

        # Источник создаётся лениво: пока шаблона нет, планировать можно и нужно.
        self._source = source
        self.pacer = Pacer(
            delay_min=settings.delay_min,
            delay_max=settings.delay_max,
            break_every=settings.break_every,
            break_min=settings.break_min,
            break_max=settings.break_max,
            max_per_hour=settings.max_requests_per_hour,
            stop_event=self.stop_event,
        )
        # Блокировка из базы подхватывается сразу: прогон не стартует в обход остывания
        blocked = self.db.source_blocked_until(SOURCE)
        if blocked:
            remaining = (blocked - datetime.now()).total_seconds() / 60
            if remaining > 0:
                log.warning("источник заблокирован ещё %.0f мин (из прошлого прогона)", remaining)
                self.pacer.block(remaining)

    # ------------------------------------------------------------------ сервис
    def __enter__(self) -> Runner:
        """Позволяет писать `with Runner() as runner:` — база закроется сама."""
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def source(self):
        """Источник данных; создаётся при первом обращении."""
        if self._source is None:
            from .sources.yandex_ssr import YandexSource

            self._source = YandexSource()
        return self._source

    def close(self) -> None:
        """Закрыть источник и базу."""
        if self._source is not None:
            self._source.close()
            self._source = None
        if self._owns_db:
            self.db.close()

    # -------------------------------------------------------------------- план
    def cells(self, cell_km: float | None = None) -> list[Cell]:
        """Сетка обхода по текущим настройкам города."""
        return build_grid(
            center_lat=self.settings.city_center_lat,
            center_lon=self.settings.city_center_lon,
            radius_km=self.settings.city_radius_km,
            cell_km=cell_km or self.settings.cell_size_km,
        )

    def estimate_requests(self, include_rubrics: bool = True) -> dict:
        """Оценка объёма до начала работы.

        Это не точное число, а порядок: по нему видно, во сколько часов
        обойдётся прогон, и можно осознанно выбрать режим.
        """
        fine = len(self.cells())
        coarse = len(self.cells(self.settings.rubric_cell_size_km))
        generic = fine * len(GENERIC_QUERIES) * PAGES_PER_QUERY
        # По рубрикам: конкретная рубрика в пределах точки и так даёт немного
        # результатов, лишние страницы чаще всего пустые
        rubrics = coarse * len(CORE_RUBRICS) * RUBRIC_PAGES if include_rubrics else 0
        # Уточнения по рубрикам добавляются по ходу: примерно треть ячеек
        # даёт новые рубрики, в среднем 5 на ячейку
        followups = int(fine * 5 * 0.35) * RUBRIC_PAGES
        total = generic + rubrics + followups

        avg_delay = (self.settings.delay_min + self.settings.delay_max) / 2
        # Перерывы и часовой потолок добавляют примерно 15%
        hours = total * avg_delay * 1.15 / 3600
        return {
            "ячеек (общие запросы)": fine,
            "ячеек (запросы по рубрикам)": coarse,
            "общих запросов": generic,
            "запросов по рубрикам": rubrics,
            "уточнений (примерно)": followups,
            "всего запросов": total,
            "времени при текущем темпе, ч": round(hours, 1),
        }

    def plan_grid(self) -> int:
        """Разложить город на ячейки и поставить разведочные запросы по каждой.

        Запросы — не название города, а общие слова («организации», «компания»).
        Это важно: поиск по названию города на этом адресе вообще не зависит от
        точки — из любой точки города приходит один и тот же список. Проверено
        замерами: «Борисов» даёт 17 одинаковых организаций из центра, юга,
        севера и запада. А «организации» в тех же точках даёт 25/29/2/5 разных
        организаций. Значит, обход по сетке работает только с такими запросами,
        и именно они показывают, какие рубрики есть в ячейке.
        """
        queued = 0
        for cell in self.cells():
            for query in GENERIC_QUERIES:
                for page in range(1, PAGES_PER_QUERY + 1):
                    queued += self.db.add_task(
                        kind=TaskKind.GENERIC,
                        source=SOURCE,
                        payload={
                            "text": query,
                            "bbox": cell.bbox,
                            "page": page,
                            "cell": cell.id,
                            "ll": f"{cell.lon},{cell.lat}",
                        },
                        dedup_key=f"generic:{cell.id}:{org_key(query)}:p{page}",
                    )
        log.info("в очередь добавлено общих запросов: %s", queued)
        return queued

    def plan_rubrics(self, rubrics: tuple[str, ...] = CORE_RUBRICS) -> int:
        """Поставить запросы «ячейка × рубрика».

        Нужны для рубрик, которые общий запрос не показал: поиск выдаёт 50
        результатов по релевантности, а не все организации рубрики в ячейке.

        Сетка здесь крупнее, чем у общих запросов (см. settings.rubric_cell_size_km):
        конкретная рубрика и так сужает выдачу, а мелкая ячейка только
        размножает число запросов.
        """
        queued = 0
        for cell in self.cells(self.settings.rubric_cell_size_km):
            for rubric in rubrics:
                for page in range(1, RUBRIC_PAGES + 1):
                    queued += self.db.add_task(
                        kind=TaskKind.RUBRIC,
                        source=SOURCE,
                        payload={
                            "text": rubric,
                            "bbox": cell.bbox,
                            "page": page,
                            "cell": cell.id,
                            "rubric": rubric,
                        },
                        dedup_key=f"rubric:{cell.id}:{org_key(rubric)}:p{page}",
                    )
        log.info("в очередь добавлено запросов по рубрикам: %s", queued)
        return queued

    def _plan_followups(self, task, cards: list) -> int:
        """Поставить уточняющие запросы по рубрикам, найденным в этой ячейке.

        Вот почему покрытие получается плотным: общий запрос показывает, что
        в ячейке есть «Аптеки» и «Автосервисы», и мы идём в неё ещё раз
        уже с этими словами. Так доходятся мелкие и редкие рубрики, которые
        в общей выдаче не попали из-за лимита в 25 результатов на страницу.
        """
        cell_id = task.payload.get("cell")
        if not cell_id:
            return 0

        counter = Counter()
        for card in cards:
            if card.category:
                counter[card.category] += 1

        queued = 0
        for rubric, _count in counter.most_common(MAX_FOLLOWUP_RUBRICS):
            for page in range(1, RUBRIC_PAGES + 1):
                queued += self.db.add_task(
                    kind=TaskKind.RUBRIC,
                    source=SOURCE,
                    payload={
                        "text": rubric,
                        "bbox": task.payload.get("bbox"),
                        "page": page,
                        "cell": cell_id,
                        "rubric": rubric,
                        "followup": True,
                    },
                    dedup_key=f"rubric:{cell_id}:{org_key(rubric)}:p{page}",
                )
        if queued:
            log.debug("ячейка %s: добавлено уточнений %s", cell_id, queued)
        return queued

    # ------------------------------------------------------------------- обход
    def sweep(
        self,
        max_requests: int | None = None,
        max_hours: float | None = None,
        followups: bool = True,
    ) -> RunStats:
        """Основной обход: разбирает очередь до конца или до лимита."""
        stats = RunStats(started_at=datetime.now().isoformat(timespec="seconds"))
        run_id = self.db.start_run()
        deadline = time.monotonic() + max_hours * 3600 if max_hours else None

        log.info(
            "старт обхода: город %s, лимит запросов %s, лимит времени %s ч",
            self.settings.city_name, max_requests or "нет", max_hours or "нет",
        )

        try:
            while True:
                # --- лимиты и остановки -------------------------------------
                if max_requests and stats.requests_made >= max_requests:
                    log.info("достигнут лимит запросов: %s", max_requests)
                    break
                if deadline and time.monotonic() > deadline:
                    log.info("достигнут лимит времени")
                    break

                if self.stop_event.is_set():
                    stats.notes.append("остановлено пользователем")
                    break

                # Источник заблокирован: ждём окончания блокировки
                if self.db.is_source_blocked(SOURCE):
                    until = self.db.source_blocked_until(SOURCE)
                    remaining = (until - datetime.now()).total_seconds() if until else 0
                    if remaining > 5:
                        log.warning("источник заблокирован, жду %.0f мин", remaining / 60)
                        self.pacer.block(remaining / 60)
                        continue
                    self.db.clear_source_block(SOURCE)

                task = self.db.next_task(SOURCE, [TaskKind.GENERIC, TaskKind.RUBRIC])
                if task is None:
                    log.info("очередь пуста")
                    break

                # --- темп ------------------------------------------------------
                try:
                    self.pacer.wait()
                except StopRequested:
                    stats.notes.append("остановлено пользователем во время паузы")
                    break

                # --- запрос ---------------------------------------------------
                payload = task.payload
                try:
                    result = self.source.search(
                        text=payload.get("text", ""),
                        bbox=payload.get("bbox"),
                        page=int(payload.get("page", 1) or 1),
                    )
                    cards = self.source.cards(result)

                    for card in cards:
                        state = self.db.upsert_org(card)
                        if state == "new":
                            stats.orgs_new += 1
                        else:
                            stats.orgs_updated += 1

                    if result.from_cache:
                        stats.requests_from_cache += 1
                    else:
                        stats.requests_made += 1

                    if followups and task.kind == TaskKind.GENERIC and cards:
                        self._plan_followups(task, cards)

                    self.db.finish_task(task.id)
                    stats.tasks_done += 1
                    self.pacer.reset_after_success()

                    # Пустая страница означает, что выдача кончилась: дальше
                    # идти незачем. Закрываем хвост, чтобы не тратить темп
                    # на заведомо пустые запросы по той же ячейке и рубрике.
                    if not cards and payload.get("cell"):
                        skipped = self.db.skip_later_pages(
                            SOURCE, task.kind, payload, int(payload.get("page", 1) or 1)
                        )
                        if skipped:
                            stats.tasks_skipped += skipped
                            log.info("выдача кончилась: закрыто лишних заданий %s", skipped)

                    log.info(
                        "[%s] %s → карточек %s (новых %s, всего %s)",
                        task.kind, payload.get("text"), len(cards),
                        stats.orgs_new, self.db.count_orgs(SOURCE),
                    )

                # --- ошибки: разные классы требуют разных действий -----------
                except CaptchaError as exc:
                    stats.captchas += 1
                    until = self.db.block_source(SOURCE, self.settings.cooldown_on_captcha_min, str(exc))
                    self.db.cooldown_task(task.id, until, str(exc))
                    self.pacer.block(self.settings.cooldown_on_captcha_min)
                    stats.notes.append(
                        f"капча в {datetime.now():%H:%M:%S}, остываем до "
                        f"{until[11:16]} — быстрых повторов не делаем"
                    )
                    log.error("капча: %s. Источник заблокирован до %s", exc, until[11:16])

                except SchemaError as exc:
                    # Структура ответа неизвестна: повтор не поможет, данные спасаем
                    stats.tasks_failed += 1
                    stats.notes.append(f"структура ответа изменилась: {exc}")
                    self.db.retry_task(task.id, str(exc), max_attempts=1)
                    log.error("структура ответа изменилась: %s", exc)
                    log.error("Останавливаю прогон. Данные сохранены. Проверьте источник: "
                              "python -m ymcontacts check")
                    break

                except NotFoundError as exc:
                    self.db.retry_task(task.id, str(exc), max_attempts=1)
                    stats.tasks_failed += 1

                except TransientError as exc:
                    self.db.retry_task(task.id, str(exc))
                    log.warning("сетевой сбой на задании %s: %s", task.id, exc)

                except Exception as exc:  # noqa: BLE001 — прогон не должен падать целиком
                    self.db.retry_task(task.id, repr(exc))
                    log.exception("непредвиденная ошибка на задании %s", task.id)
                    time.sleep(3)

        finally:
            stats.finished_at = datetime.now().isoformat(timespec="seconds")
            self._save_run(run_id, stats)

        log.info(
            "обход завершён: запросов %s (из кэша %s), новых организаций %s, заданий %s, сбоев %s",
            stats.requests_made, stats.requests_from_cache, stats.orgs_new,
            stats.tasks_done, stats.tasks_failed,
        )
        return stats

    # -------------------------------------------------------------------- сайты
    def run_sites(self, limit: int | None = None) -> RunStats:
        """Этап 2: обойти сайты собранных организаций за почтой.

        Идём пулом потоков, но с ограничением: на одном доме — один запрос
        за раз, и общий потолок невысокий.
        """
        stats = RunStats(started_at=datetime.now().isoformat(timespec="seconds"))
        run_id = self.db.start_run()

        rows = self.db.org_rows_with_site(SOURCE)
        if limit:
            rows = rows[:limit]
        log.info("сайтов к обработке: %s", len(rows))

        crawler = SiteCrawler(
            max_pages=self.settings.max_pages_per_site,
            pause_between_pages=(self.settings.site_delay_min, self.settings.site_delay_max),
            timeout=self.settings.site_timeout,
        )

        # SQLite плохо переносит параллельную запись: записи сериализуем локом
        write_lock = threading.Lock()

        with ThreadPoolExecutor(max_workers=self.settings.site_workers) as pool:
            futures = {
                pool.submit(self._process_site, crawler, row): row
                for row in rows
            }
            for index, future in enumerate(as_completed(futures), start=1):
                if self.stop_event.is_set():
                    log.warning("остановлено пользователем: %s из %s сайтов обработано", index - 1, len(rows))
                    stats.notes.append("остановлено пользователем")
                    break
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    log.warning("сайт не обработан: %s", exc)
                    stats.tasks_failed += 1
                    continue

                stats.sites_processed += 1
                if result.skipped:
                    log.debug("сайт пропущен: %s (%s)", result.url, result.skipped)
                    continue

                if result.emails or result.messengers:
                    with write_lock:
                        self.db.save_contacts(
                            source=SOURCE,
                            external_id=result.external_id,
                            emails=result.emails,
                            messengers=result.messengers,
                        )
                    stats.emails_found += len(result.emails)

                if index % 25 == 0:
                    log.info(
                        "обработано сайтов: %s/%s, найдено почт: %s",
                        index, len(rows), stats.emails_found,
                    )

        stats.finished_at = datetime.now().isoformat(timespec="seconds")
        self._save_run(run_id, stats)
        log.info(
            "сайты обработаны: %s, почт найдено: %s", stats.sites_processed, stats.emails_found
        )
        return stats

    def _process_site(self, crawler: SiteCrawler, row):
        """Обработать один сайт. Отдельный метод — чтобы поймать исключение в потоке."""
        try:
            return crawler.crawl(row["website"], external_id=row["external_id"])
        except Exception as exc:  # noqa: BLE001
            log.debug("ошибка обхода %s: %s", row["website"], exc)
            from .sites import SiteResult

            return SiteResult(url=row["website"], external_id=row["external_id"], error=str(exc)[:200])

    # ----------------------------------------------------------------- отчёты
    def _save_run(self, run_id: int, stats: RunStats) -> None:
        """Сохранить итоги прогона в базу."""
        from dataclasses import asdict

        self.db.finish_run(run_id, asdict(stats))

    def report(self) -> dict:
        """Сводка: что в базе, что в очереди, что заблокировано."""
        blocked = self.db.source_blocked_until(SOURCE)
        return {
            **self.db.stats(SOURCE),
            "blocked_until": blocked.isoformat(timespec="seconds") if blocked else None,
            "raw_files": len(list(self.settings.raw_dir.glob("*.json.gz"))),
        }
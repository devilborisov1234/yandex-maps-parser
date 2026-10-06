"""Слой хранения: SQLite.

Почему SQLite, а не CSV-файл на лету:
  - очередь заданий должна переживать перезапуск программы;
  - повторный запуск не должен плодить дубли (апсерт по ключу);
  - можно сравнивать прогоны между собой (история изменений).

Схема версионируется: список MIGRATIONS применяется по порядку, текущая версия
хранится в таблице meta. Это позволяет менять структуру без потери данных.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .models import OrgCard, Task, TaskKind, TaskStatus

log = get_logger(__name__)

SCHEMA_VERSION = 1

# JSON-поля храним строками: проще, чем отдельные таблицы-связи,
# а данных на одну организацию немного.
JSON_FIELDS = {"categories", "phones", "socials", "emails", "messengers"}

MIGRATIONS: list[str] = [
    # --- миграция 1: базовая схема -----------------------------------------
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );

    -- Карточки организаций. Ключ: источник + идентификатор в источнике.
    CREATE TABLE IF NOT EXISTS org_cards (
        source        TEXT NOT NULL,
        external_id   TEXT NOT NULL,
        name          TEXT,
        category      TEXT,
        categories    TEXT,
        address       TEXT,
        phones        TEXT,
        website       TEXT,
        socials       TEXT,
        emails        TEXT,
        messengers    TEXT,
        lat           REAL,
        lon           REAL,
        hours         TEXT,
        rating        REAL,
        reviews_count INTEGER,
        url           TEXT,
        group_key     TEXT,
        first_seen    TEXT,
        last_seen     TEXT,
        PRIMARY KEY (source, external_id)
    );
    CREATE INDEX IF NOT EXISTS idx_orgs_group ON org_cards(group_key);
    CREATE INDEX IF NOT EXISTS idx_orgs_site  ON org_cards(source, website);
    CREATE INDEX IF NOT EXISTS idx_orgs_seen  ON org_cards(last_seen);

    -- Очередь заданий. dedup_key не даёт создать одно и то же задание дважды.
    CREATE TABLE IF NOT EXISTS tasks (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        kind           TEXT NOT NULL,
        source         TEXT NOT NULL,
        dedup_key      TEXT NOT NULL UNIQUE,
        payload        TEXT NOT NULL,
        status         TEXT NOT NULL,
        attempts       INTEGER NOT NULL DEFAULT 0,
        last_error     TEXT,
        cooldown_until TEXT,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, id);
    CREATE INDEX IF NOT EXISTS idx_tasks_kind   ON tasks(kind, status);

    -- Состояние источников: когда и почему заблокирован.
    CREATE TABLE IF NOT EXISTS source_state (
        source               TEXT PRIMARY KEY,
        blocked_until        TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        last_error           TEXT,
        updated_at           TEXT
    );

    -- Индекс кэша сырых ответов (файлы лежат в data/raw).
    CREATE TABLE IF NOT EXISTS raw_index (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        source     TEXT,
        kind       TEXT,
        url        TEXT,
        cache_key  TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        bytes      INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_raw_key ON raw_index(cache_key);

    -- Журнал прогонов.
    CREATE TABLE IF NOT EXISTS runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        stats       TEXT
    );
    """,
]


def now_iso() -> str:
    """Текущее время в ISO-формате с секундами — единый формат времени в базе."""
    return datetime.now().isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    """Сериализация в JSON без экранирования кириллицы."""
    return json.dumps(value, ensure_ascii=False)


def _loads(value: str | None, default: Any) -> Any:
    """Обратная операция к _dumps; битое значение не должно ронять прогон."""
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class Database:
    """Обёртка над SQLite-соединением. Один экземпляр на процесс."""

    def __init__(self, path: Path | str) -> None:
        from .config import settings

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Каталог для кэша сырых ответов — нужен методам raw_cached/save_raw
        self.raw_dir = settings.raw_dir
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: этап сайтов работает в пуле потоков.
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._configure()
        self._migrate()
        self._recover_interrupted()

    # ------------------------------------------------------------ базовая обвязка
    def _configure(self) -> None:
        """PRAGMA-настройки: WAL переживает падение, synchronous — баланс скорости."""
        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")  # не блокирует чтение при записи
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
        self.conn.commit()

    def _migrate(self) -> None:
        """Применить недостающие миграции."""
        cur = self.conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        row = cur.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        current = int(row["value"]) if row else 0

        for index in range(current, len(MIGRATIONS)):
            cur.executescript(MIGRATIONS[index])
            log.info("применена миграция %s", index + 1)

        cur.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(len(MIGRATIONS)),),
        )
        self.conn.commit()

    def _recover_interrupted(self) -> None:
        """Задания, оставшиеся в статусе running, — это падение или Ctrl+C.

        Возвращаем их в очередь: иначе они потерялись бы навсегда.
        """
        cur = self.conn.execute(
            "UPDATE tasks SET status=?, cooldown_until=NULL, updated_at=? WHERE status=?",
            (TaskStatus.RETRY, now_iso(), TaskStatus.RUNNING),
        )
        if cur.rowcount:
            self.conn.commit()
            log.warning("восстановлено прерванных заданий: %s", cur.rowcount)

    def close(self) -> None:
        """Закрыть соединение корректно."""
        self.conn.commit()
        self.conn.close()

    # -------------------------------------------------------------- организации
    def upsert_org(self, card: OrgCard) -> str:
        """Записать карточку. Возвращает 'new' или 'updated'.

        Апсерт: повторный прогон обновляет карточку и двигает last_seen,
        но не создаёт дубль. Это и есть основа идемпотентности.
        """
        seen = now_iso()
        card.first_seen = card.first_seen or seen
        card.last_seen = seen
        row = card.to_row()

        columns = [k for k in row if k not in ("first_seen",)]
        values = [_dumps(row[k]) if k in JSON_FIELDS else row[k] for k in columns]
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{c}=excluded.{c}" for c in columns if c not in ("source", "external_id"))

        exists = self.conn.execute(
            "SELECT 1 FROM org_cards WHERE source=? AND external_id=?",
            (card.source, card.external_id),
        ).fetchone()

        self.conn.execute(
            f"INSERT INTO org_cards ({', '.join(columns)}, first_seen) VALUES ({placeholders}, ?) "
            f"ON CONFLICT(source, external_id) DO UPDATE SET {updates}",
            (*values, card.first_seen),
        )
        self.conn.commit()
        return "updated" if exists else "new"

    def save_contacts(
        self,
        source: str,
        external_id: str,
        emails: list[str] | None = None,
        messengers: dict[str, str] | None = None,
        site_status: str | None = None,
    ) -> None:
        """Дописать найденные на сайте почты и мессенджеры к существующей карточке."""
        updates: list[str] = []
        values: list[Any] = []

        if emails is not None:
            old = self._get_json(source, external_id, "emails", [])
            merged = list(dict.fromkeys([*_loads(old, []), *emails]))
            updates.append("emails=?")
            values.append(_dumps(merged))

        if messengers is not None:
            old = self._get_json(source, external_id, "messengers", {})
            merged = {**_loads(old, {}), **messengers}
            updates.append("messengers=?")
            values.append(_dumps(merged))

        if not updates:
            return

        self.conn.execute(
            f"UPDATE org_cards SET {', '.join(updates)}, last_seen=? WHERE source=? AND external_id=?",
            (*values, now_iso(), source, external_id),
        )
        self.conn.commit()

    def _get_json(self, source: str, external_id: str, column: str, default: Any) -> Any:
        row = self.conn.execute(
            f"SELECT {column} AS v FROM org_cards WHERE source=? AND external_id=?",
            (source, external_id),
        ).fetchone()
        return row["v"] if row else default

    def org_rows(self, source: str = "yandex") -> list[sqlite3.Row]:
        """Все карточки источника."""
        return list(self.conn.execute("SELECT * FROM org_cards WHERE source=?", (source,)))

    def org_rows_with_site(self, source: str = "yandex") -> list[sqlite3.Row]:
        """Карточки, у которых есть сайт и ещё не заведён задание на обход."""
        done = {
            _loads(r["payload"], {}).get("external_id")
            for r in self.conn.execute(
                "SELECT payload FROM tasks WHERE kind=? AND status=?", (TaskKind.SITE, TaskStatus.DONE)
            )
        }
        return [
            row
            for row in self.org_rows(source)
            if row["website"] and row["external_id"] not in done
        ]

    def orgs_with_contacts(self, source: str = "yandex") -> list[dict]:
        """Готовые записи для экспорта: JSON-поля разобраны обратно в объекты."""
        result: list[dict] = []
        for row in self.org_rows(source):
            result.append(
                {
                    "name": row["name"] or "",
                    "category": row["category"] or "",
                    "categories": _loads(row["categories"], []),
                    "address": row["address"] or "",
                    "phones": _loads(row["phones"], []),
                    "website": row["website"] or "",
                    "socials": _loads(row["socials"], {}),
                    "emails": _loads(row["emails"], []),
                    "messengers": _loads(row["messengers"], {}),
                    "lat": row["lat"],
                    "lon": row["lon"],
                    "url": row["url"] or "",
                    "group_key": row["group_key"] or "",
                    "last_seen": row["last_seen"] or "",
                    "external_id": row["external_id"],
                }
            )
        return result

    # ------------------------------------------------------------------ очередь
    def add_task(
        self,
        kind: TaskKind,
        source: str,
        payload: dict,
        dedup_key: str,
        status: TaskStatus = TaskStatus.PENDING,
    ) -> bool:
        """Поставить задание в очередь. False — такое уже существует."""
        stamp = now_iso()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO tasks (kind, source, dedup_key, payload, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (TaskKind(kind), source, dedup_key, _dumps(payload), TaskStatus(status), stamp, stamp),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def add_tasks(self, tasks: Iterable[tuple[TaskKind, str, dict, str]]) -> int:
        """Массовая постановка в очередь. Возвращает число новых заданий."""
        added = 0
        for kind, source, payload, dedup_key in tasks:
            if self.add_task(kind, source, payload, dedup_key):
                added += 1
        return added

    def next_task(self, source: str, kinds: Iterable[TaskKind]) -> Task | None:
        """Взять следующее задание в работу. Возвращает None, если очередь пуста.

        Задание переводится в статус running — при падении процесса
        _recover_interrupted вернёт его в retry.
        """
        kinds = [TaskKind(k) for k in kinds]
        placeholders = ", ".join("?" for _ in kinds)
        # Условие по cooldown действует и для retry, и для cooldown:
        # отложенное задание не должно выдаваться раньше указанного момента.
        row = self.conn.execute(
            f"""
            SELECT * FROM tasks
            WHERE source = ?
              AND kind IN ({placeholders})
              AND status IN (?, ?, ?)
              AND (cooldown_until IS NULL OR cooldown_until <= ?)
            ORDER BY attempts ASC, id ASC
            LIMIT 1
            """,
            (
                source,
                *kinds,
                TaskStatus.PENDING,
                TaskStatus.RETRY,
                TaskStatus.COOLDOWN,
                now_iso(),
            ),
        ).fetchone()
        if not row:
            return None

        self.conn.execute(
            "UPDATE tasks SET status=?, attempts=attempts+1, updated_at=? WHERE id=?",
            (TaskStatus.RUNNING, now_iso(), row["id"]),
        )
        self.conn.commit()

        return Task(
            id=row["id"],
            kind=TaskKind(row["kind"]),
            source=row["source"],
            dedup_key=row["dedup_key"],
            payload=_loads(row["payload"], {}),
            status=TaskStatus.RUNNING,
            attempts=row["attempts"] + 1,
        )

    def finish_task(self, task_id: int) -> None:
        """Задание выполнено."""
        self.conn.execute(
            "UPDATE tasks SET status=?, cooldown_until=NULL, updated_at=? WHERE id=?",
            (TaskStatus.DONE, now_iso(), task_id),
        )
        self.conn.commit()

    def skip_later_pages(self, source: str, kind: str, payload: dict, from_page: int) -> int:
        """Закрыть все страницы той же выдачи, что и дальше.

        Выдача упорядочена: если страница вернула пусто, следующих по этому же
        запросу тоже не будет. Без этого каждая ячейка в одиночку «съедала» бы
        лишние запросы впустую.

        Важно: сверяется не только ячейка, но и сам запрос — у запроса
        «организации» и «компания» выдачи разные, и закрывать надо только
        хвост своего запроса. Возвращает число закрытых заданий.
        """
        cur = self.conn.execute(
            """
            UPDATE tasks
               SET status=?, cooldown_until=NULL, updated_at=?
             WHERE source=? AND kind=? AND status=?
               AND json_extract(payload, '$.cell') = ?
               AND json_extract(payload, '$.text') = ?
               AND COALESCE(json_extract(payload, '$.rubric'), '') = COALESCE(?, '')
               AND CAST(COALESCE(json_extract(payload, '$.page'), 1) AS INTEGER) > ?
            """,
            (
                TaskStatus.DONE,
                now_iso(),
                source,
                kind,
                TaskStatus.PENDING,
                payload.get("cell"),
                payload.get("text"),
                payload.get("rubric"),
                int(from_page),
            ),
        )
        self.conn.commit()
        return cur.rowcount or 0

    def retry_task(self, task_id: int, error: str, max_attempts: int = 3, delay_sec: int = 30) -> bool:
        """Пометить задание на повтор. False — попытки исчерпаны, задание провалено."""
        row = self.conn.execute("SELECT attempts FROM tasks WHERE id=?", (task_id,)).fetchone()
        attempts = row["attempts"] if row else max_attempts + 1

        if attempts >= max_attempts:
            self.conn.execute(
                "UPDATE tasks SET status=?, last_error=?, updated_at=? WHERE id=?",
                (TaskStatus.FAILED, error[:500], now_iso(), task_id),
            )
            self.conn.commit()
            log.error("задание %s провалено после %s попыток: %s", task_id, attempts, error[:200])
            return False

        # Небольшая пауза перед повтором: агрессивный ретрай тоже палевен.
        until = (datetime.now() + timedelta(seconds=delay_sec * attempts)).isoformat(timespec="seconds")
        self.conn.execute(
            "UPDATE tasks SET status=?, last_error=?, cooldown_until=?, updated_at=? WHERE id=?",
            (TaskStatus.RETRY, error[:500], until, now_iso(), task_id),
        )
        self.conn.commit()
        return True

    def cooldown_task(self, task_id: int, until_iso: str, error: str) -> None:
        """Отложить задание до указанного момента (после блокировки источника)."""
        self.conn.execute(
            "UPDATE tasks SET status=?, cooldown_until=?, last_error=?, updated_at=? WHERE id=?",
            (TaskStatus.COOLDOWN, until_iso, error[:500], now_iso(), task_id),
        )
        self.conn.commit()

    def queue_stats(self, source: str = "yandex") -> dict[str, int]:
        """Сколько заданий в каком статусе — для отчёта и понимания прогресса."""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks WHERE source=? GROUP BY status", (source,)
        )
        return {row["status"]: row["n"] for row in rows}

    # --------------------------------------------------------------- источники
    def source_blocked_until(self, source: str) -> datetime | None:
        """До какого момента источник заблокирован (None — не заблокирован)."""
        row = self.conn.execute(
            "SELECT blocked_until FROM source_state WHERE source=?", (source,)
        ).fetchone()
        if not row or not row["blocked_until"]:
            return None
        try:
            return datetime.fromisoformat(row["blocked_until"])
        except ValueError:
            return None

    def is_source_blocked(self, source: str) -> bool:
        """Проверка блокировки. Работает и после перезапуска — состояние в БД."""
        until = self.source_blocked_until(source)
        return bool(until and until > datetime.now())

    def block_source(self, source: str, minutes: int, error: str) -> str:
        """Заблокировать источник на N минут. Возвращает время окончания блокировки."""
        until = (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        self.conn.execute(
            """
            INSERT INTO source_state (source, blocked_until, consecutive_failures, last_error, updated_at)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                blocked_until = excluded.blocked_until,
                consecutive_failures = source_state.consecutive_failures + 1,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            """,
            (source, until, error[:500], now_iso()),
        )
        self.conn.commit()
        return until

    def clear_source_block(self, source: str) -> None:
        """Снять блокировку (после успешной проверки в браузере)."""
        self.conn.execute(
            "UPDATE source_state SET blocked_until=NULL, consecutive_failures=0, updated_at=? WHERE source=?",
            (now_iso(), source),
        )
        self.conn.commit()

    # ------------------------------------------------------------------- прогоны
    def start_run(self) -> int:
        """Открыть запись прогона, вернуть id."""
        cur = self.conn.execute("INSERT INTO runs (started_at) VALUES (?)", (now_iso(),))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, stats: dict) -> None:
        """Закрыть прогон и сохранить статистику."""
        self.conn.execute(
            "UPDATE runs SET finished_at=?, stats=? WHERE id=?",
            (now_iso(), _dumps(stats), run_id),
        )
        self.conn.commit()

    def last_run(self) -> sqlite3.Row | None:
        """Последний прогон — для команды report."""
        return self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()

    # -------------------------------------------------------------------- кэш
    def raw_cached(self, cache_key: str, max_age_hours: int = 720) -> Path | None:
        """Путь к файлу кэша, если он свежий. max_age 720ч = 30 суток."""
        row = self.conn.execute(
            "SELECT cache_key, fetched_at FROM raw_index WHERE cache_key=? ORDER BY id DESC LIMIT 1",
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        try:
            fetched = datetime.fromisoformat(row["fetched_at"])
        except ValueError:
            return None
        if datetime.now() - fetched > timedelta(hours=max_age_hours):
            return None
        path = self.raw_dir / f"{cache_key}.json.gz"
        return path if path.exists() else None

    def save_raw_index(self, source: str, kind: str, url: str, cache_key: str, size: int) -> None:
        """Записать в индекс факт сохранения сырого ответа."""
        self.conn.execute(
            "INSERT INTO raw_index (source, kind, url, cache_key, fetched_at, bytes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source, kind, url, cache_key, now_iso(), size),
        )
        self.conn.commit()

    # Удобные счётчики для отчёта
    def count_orgs(self, source: str = "yandex") -> int:
        """Сколько уникальных карточек в базе."""
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM org_cards WHERE source=?", (source,)
        ).fetchone()["n"]

    def count_groups(self, source: str = "yandex") -> int:
        """Сколько организаций после склейки филиалов."""
        return self.conn.execute(
            "SELECT COUNT(DISTINCT group_key) AS n FROM org_cards WHERE source=?", (source,)
        ).fetchone()["n"]

    def stats(self, source: str = "yandex") -> dict[str, Any]:
        """Сводка по базе: карточки, контакты, очередь."""
        row = self.conn.execute(
            """
            SELECT
                COUNT(*)                                              AS cards,
                SUM(CASE WHEN phones  IS NOT NULL AND phones  != '[]' THEN 1 ELSE 0 END) AS with_phone,
                SUM(CASE WHEN emails  IS NOT NULL AND emails  != '[]' THEN 1 ELSE 0 END) AS with_email,
                SUM(CASE WHEN website IS NOT NULL AND website != ''    THEN 1 ELSE 0 END) AS with_site
            FROM org_cards WHERE source=?
            """,
            (source,),
        ).fetchone()
        return {
            "cards": row["cards"] or 0,
            "groups": self.count_groups(source),
            "with_phone": row["with_phone"] or 0,
            "with_email": row["with_email"] or 0,
            "with_site": row["with_site"] or 0,
            "tasks": self.queue_stats(source),
        }


def open_db(path: Path | str | None = None) -> Database:
    """Открыть базу с настройками по умолчанию."""
    from .config import settings

    return Database(path or settings.db_path)
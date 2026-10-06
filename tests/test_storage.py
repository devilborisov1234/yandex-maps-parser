"""Тесты хранилища: идемпотентность, очередь, восстановление после падения.

Именно эти три свойства отличают нормальный сбор от «скрипта, который
при втором запуске всё ломает».
"""

from datetime import datetime, timedelta

from ymcontacts.models import OrgCard, TaskKind, TaskStatus
from ymcontacts.storage import Database


def make_card(external_id: str = "org-1", name: str = "Аптека", **overrides) -> OrgCard:
    """Карточка для тестов. overrides позволяют менять любое поле."""
    fields = {
        "source": "yandex",
        "external_id": external_id,
        "name": name,
        "category": "Аптеки",
        "categories": ["Аптеки"],
        "address": "г. Борисов, ул. Ленина, 1",
        "phones": ["+375291234567"],
        "website": "https://shop.by",
        "emails": [],
        "socials": {"vk": "https://vk.com/shop"},
        "lat": 54.2277,
        "lon": 28.485,
        "group_key": "аптека",
    }
    fields.update(overrides)
    return OrgCard(**fields)


class TestUpsert:
    def test_new_then_update(self, tmp_path):
        """Второй прогон обновляет карточку, а не создаёт дубль."""
        db = Database(tmp_path / "test.db")
        assert db.upsert_org(make_card()) == "new"
        assert db.upsert_org(make_card(phones=["+375291234567", "+375291234568"])) == "updated"
        assert db.count_orgs() == 1
        db.close()

    def test_first_seen_preserved(self, tmp_path):
        """first_seen не перетирается при повторном прогоне."""
        db = Database(tmp_path / "test.db")
        db.upsert_org(make_card())
        first = db.org_rows()[0]["first_seen"]
        db.upsert_org(make_card())
        assert db.org_rows()[0]["first_seen"] == first
        db.close()

    def test_save_contacts_merges(self, tmp_path):
        """Этап сайтов дописывает почты, не затирая то, что уже было."""
        db = Database(tmp_path / "test.db")
        db.upsert_org(make_card())
        db.save_contacts("yandex", "org-1", emails=["a@shop.by"])
        db.save_contacts("yandex", "org-1", emails=["b@shop.by"])
        assert db.orgs_with_contacts()[0]["emails"] == ["a@shop.by", "b@shop.by"]
        db.close()

    def test_stats(self, tmp_path):
        db = Database(tmp_path / "test.db")
        db.upsert_org(make_card())
        db.upsert_org(make_card("org-2", "Кафе"))
        stats = db.stats()
        assert stats["cards"] == 2
        assert stats["with_phone"] == 2
        db.close()


class TestQueue:
    def test_add_is_idempotent(self, tmp_path):
        """Одно задание нельзя поставить в очередь дважды."""
        db = Database(tmp_path / "test.db")
        assert db.add_task(TaskKind.GENERIC, "yandex", {"text": "Борисов"}, "generic:c00") is True
        assert db.add_task(TaskKind.GENERIC, "yandex", {"text": "Борисов"}, "generic:c00") is False
        assert db.queue_stats()["pending"] == 1
        db.close()

    def test_take_in_order(self, tmp_path):
        db = Database(tmp_path / "test.db")
        for index in range(3):
            db.add_task(TaskKind.GENERIC, "yandex", {"i": index}, f"g{index}")
        first = db.next_task("yandex", [TaskKind.GENERIC])
        assert first.payload["i"] == 0
        assert first.status == TaskStatus.RUNNING
        db.finish_task(first.id)
        assert db.next_task("yandex", [TaskKind.GENERIC]).payload["i"] == 1
        db.close()

    def test_empty_queue(self, tmp_path):
        db = Database(tmp_path / "test.db")
        assert db.next_task("yandex", [TaskKind.GENERIC, TaskKind.RUBRIC]) is None
        db.close()

    def test_retry_respects_cooldown(self, tmp_path):
        """Задание на паузе не выдаётся раньше срока."""
        db = Database(tmp_path / "test.db")
        db.add_task(TaskKind.GENERIC, "yandex", {}, "g0")
        task = db.next_task("yandex", [TaskKind.GENERIC])
        db.retry_task(task.id, "сетевой сбой", max_attempts=3, delay_sec=600)
        assert db.next_task("yandex", [TaskKind.GENERIC]) is None
        db.close()

    def test_max_attempts_gives_up(self, tmp_path):
        """После трёх попыток задание проваливается, а не крутится вечно."""
        db = Database(tmp_path / "test.db")
        db.add_task(TaskKind.GENERIC, "yandex", {}, "g0")

        # Первая и вторая попытки: задание возвращается в очередь.
        for _ in range(2):
            task = db.next_task("yandex", [TaskKind.GENERIC])
            assert task is not None
            db.conn.execute("UPDATE tasks SET cooldown_until=NULL WHERE id=?", (task.id,))
            db.conn.commit()
            assert db.retry_task(task.id, "ошибка", max_attempts=3, delay_sec=0) is True

        # Третья попытка: попытки исчерпаны, задание провалено.
        task = db.next_task("yandex", [TaskKind.GENERIC])
        assert task is not None
        assert db.retry_task(task.id, "ошибка", max_attempts=3, delay_sec=0) is False
        assert db.next_task("yandex", [TaskKind.GENERIC]) is None
        assert db.queue_stats().get("failed", 0) == 1
        db.close()


class TestRecovery:
    def test_running_tasks_recovered_after_crash(self, tmp_path):
        """Задание, оставшееся в running после падения, возвращается в очередь."""
        path = tmp_path / "test.db"
        db = Database(path)
        db.add_task(TaskKind.GENERIC, "yandex", {"text": "Борисов"}, "g0")
        db.next_task("yandex", [TaskKind.GENERIC])  # берём в работу и НЕ закрываем
        db.close()  # имитируем падение процесса

        db2 = Database(path)
        recovered = db2.next_task("yandex", [TaskKind.GENERIC])
        assert recovered is not None
        assert recovered.payload["text"] == "Борисов"
        db2.close()

    def test_migrations_are_idempotent(self, tmp_path):
        """Повторное открытие не ломает схему."""
        path = tmp_path / "test.db"
        Database(path).close()
        db = Database(path)
        assert db.count_orgs() == 0
        db.close()


class TestSourceState:
    def test_block_survives_restart(self, tmp_path):
        """Блокировка после капчи хранится в базе и переживает перезапуск."""
        path = tmp_path / "test.db"
        db = Database(path)
        assert db.is_source_blocked("yandex") is False
        db.block_source("yandex", 45, "капча")
        assert db.is_source_blocked("yandex") is True
        db.close()

        db2 = Database(path)
        assert db2.is_source_blocked("yandex") is True
        db2.clear_source_block("yandex")
        assert db2.is_source_blocked("yandex") is False
        db2.close()

    def test_expired_block_is_not_blocking(self, tmp_path):
        db = Database(tmp_path / "test.db")
        past = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
        db.block_source("yandex", 1, "капча")
        db.conn.execute("UPDATE source_state SET blocked_until=? WHERE source=?", (past, "yandex"))
        db.conn.commit()
        assert db.is_source_blocked("yandex") is False
        db.close()
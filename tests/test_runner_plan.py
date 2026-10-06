"""Тесты планировщика обхода.

Главное, что здесь проверяется: в очередь попадает не одна страница выдачи на
ячейку, а несколько, и ключи у них разные — иначе вторая и третья страницы
никогда не запрашивались бы, а обход молча терял бы организации.

Тесты идут по-настоящему: с временной базой и настоящей сеткой, но без сети —
проверяется только планирование.
"""

from __future__ import annotations

import pytest

from ymcontacts.config import settings
from ymcontacts.models import OrgCard, SearchPage, TaskKind, TaskStatus
from ymcontacts.runner import (
    CORE_RUBRICS,
    GENERIC_QUERIES,
    MAX_FOLLOWUP_RUBRICS,
    PAGES_PER_QUERY,
    RUBRIC_PAGES,
    SOURCE,
    Runner,
)
from ymcontacts.storage import Database


def make_card(name: str, category: str = "Аптеки") -> OrgCard:
    return OrgCard(
        source=SOURCE,
        external_id=f"id-{name}",
        name=name,
        category=category,
        categories=[category],
        address="Борисов",
        phones=["+375291234567"],
    )


def fetch_tasks(db: Database) -> list[dict]:
    """Все задания из очереди с разобранным JSON-полезным нагрузкой."""
    import json

    rows = db.conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    tasks = []
    for row in rows:
        task = dict(row)
        task["payload"] = json.loads(task["payload"])
        tasks.append(task)
    return tasks


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "raw_dir", tmp_path / "raw")
    monkeypatch.setattr(settings, "db_path", tmp_path / "state.db")
    monkeypatch.setattr(settings, "city_center_lon", 28.485)
    monkeypatch.setattr(settings, "city_center_lat", 54.2277)
    monkeypatch.setattr(settings, "city_radius_km", 5.0)
    monkeypatch.setattr(settings, "cell_size_km", 3.0)
    monkeypatch.setattr(settings, "rubric_cell_size_km", 4.0)
    # Темп и лимиты на планирование не влияют, но проверяем, что они не мешают
    monkeypatch.setattr(settings, "delay_min", 0.0)
    monkeypatch.setattr(settings, "delay_max", 0.0)

    instance = Runner(db=Database(settings.db_path))
    yield instance
    instance.close()


class TestPlanGrid:
    """Общие запросы-разведчики: столько запросов, сколько ячеек × запросов × страниц."""

    def test_every_cell_gets_every_page(self, runner):
        queued = runner.plan_grid()
        expected = len(runner.cells()) * len(GENERIC_QUERIES) * PAGES_PER_QUERY
        assert queued == expected
        assert len(fetch_tasks(runner.db)) == expected

    def test_page_in_payload(self, runner):
        runner.plan_grid()
        pages = sorted({task["payload"]["page"] for task in fetch_tasks(runner.db)})
        assert pages == list(range(1, PAGES_PER_QUERY + 1))

    def test_pages_have_different_dedup_keys(self, runner):
        runner.plan_grid()
        keys = [task["dedup_key"] for task in fetch_tasks(runner.db)]
        # ключ обязан различаться страницей, иначе страницы отбрасываются как дубли
        assert len(set(keys)) == len(keys)
        suffixes = {key.rsplit(":p", 1)[1] for key in keys}
        assert suffixes == {str(page) for page in range(1, PAGES_PER_QUERY + 1)}

    def test_queries_and_pages_do_not_collide(self, runner):
        # два разных запроса на одной ячейке — это разные задания
        runner.plan_grid()
        tasks = fetch_tasks(runner.db)
        keys = [task["dedup_key"] for task in tasks]
        assert len(set(keys)) == len(keys) == len(runner.cells()) * len(GENERIC_QUERIES) * PAGES_PER_QUERY
        one_cell = next(cell.id for cell in runner.cells())
        texts = {task["payload"]["text"] for task in tasks if task["payload"]["cell"] == one_cell}
        assert texts == set(GENERIC_QUERIES)

    def test_city_name_not_used_as_query(self, runner):
        # название города на этом адресе не зависит от точки: из любой точки
        # приходит один и тот же список, обход по сетке с ним бессмыслен
        runner.plan_grid()
        texts = {task["payload"]["text"] for task in fetch_tasks(runner.db)}
        assert texts == set(GENERIC_QUERIES)
        assert settings.city_name not in texts

    def test_planning_twice_does_not_duplicate(self, runner):
        first = runner.plan_grid()
        second = runner.plan_grid()
        assert first > 0
        assert second == 0
        assert len(fetch_tasks(runner.db)) == first

    def test_payload_carries_cell_and_point(self, runner):
        runner.plan_grid()
        for task in fetch_tasks(runner.db):
            assert task["payload"]["cell"]
            assert "," in task["payload"]["ll"]
            assert task["kind"] == TaskKind.GENERIC.value

    def test_payload_bbox_matches_cell(self, runner):
        runner.plan_grid()
        cells = {cell.id: cell for cell in runner.cells()}
        for task in fetch_tasks(runner.db):
            assert task["payload"]["bbox"] == cells[task["payload"]["cell"]].bbox


class TestPlanRubrics:
    """Запросы по рубрикам: отдельная пагинация и отдельные ключи."""

    def test_core_rubrics_per_cell_and_page(self, runner):
        queued = runner.plan_rubrics(CORE_RUBRICS)
        expected = (
            len(runner.cells(settings.rubric_cell_size_km)) * len(CORE_RUBRICS) * RUBRIC_PAGES
        )
        assert queued == expected

    def test_rubric_pages_not_more_than_generic(self, runner):
        # в пределах точки конкретная рубрика быстро исчерпывается
        assert RUBRIC_PAGES <= PAGES_PER_QUERY

    def test_rubric_name_in_payload_and_key(self, runner):
        runner.plan_rubrics(["аптека", "автосервис"])
        tasks = fetch_tasks(runner.db)
        assert {task["payload"]["rubric"] for task in tasks} == {"аптека", "автосервис"}
        assert all(task["kind"] == TaskKind.RUBRIC.value for task in tasks)
        assert all(task["payload"]["rubric"] in task["dedup_key"] for task in tasks)

    def test_pages_of_same_rubric_do_not_collide(self, runner):
        runner.plan_rubrics(["аптека"])
        keys = [task["dedup_key"] for task in fetch_tasks(runner.db)]
        expected = len(runner.cells(settings.rubric_cell_size_km)) * RUBRIC_PAGES
        assert len(keys) == expected
        assert len(set(keys)) == expected

    def test_empty_rubric_list_plans_nothing(self, runner):
        assert runner.plan_rubrics([]) == 0


class TestFollowups:
    """Уточнения: рубрики, найденные в общем запросе, обходятся отдельно."""

    def _generic_task(self, runner, cell: str = "cell-1") -> dict:
        runner.db.add_task(
            kind=TaskKind.GENERIC,
            source=SOURCE,
            payload={"text": "Борисов", "cell": cell, "bbox": "28.4,54.2~28.5,54.3", "page": 1},
            dedup_key="generic:cell-1:p1",
        )
        return fetch_tasks(runner.db)[0]

    def test_new_rubrics_are_queued(self, runner):
        task = self._generic_task(runner)
        cards = [
            make_card("Дентал", "Стоматологическая клиника"),
            make_card("Шиномонтаж", "Шиномонтаж"),
        ]
        queued = runner._plan_followups(_StubTask(task), cards)
        assert queued == 2 * RUBRIC_PAGES

    def test_repeated_rubric_counted_once(self, runner):
        task = self._generic_task(runner)
        cards = [make_card("Дентал", "Стоматологическая клиника")] * 4
        queued = runner._plan_followups(_StubTask(task), cards)
        assert queued == RUBRIC_PAGES

    def test_followup_limited_per_cell(self, runner):
        task = self._generic_task(runner)
        rubrics = [
            "шиномонтаж", "стоматология", "парикмахерская", "пекарня", "аптека",
            "автосервис", "банк", "ресторан", "гостиница", "спортзал", "школа",
            "детский сад", "мебель", "обувь", "книжный магазин", "цветы",
            "тату", "фотограф", "ветеринар", "психолог",
        ]
        cards = [make_card(f"Орг {i}", rubric) for i, rubric in enumerate(rubrics)]
        queued = runner._plan_followups(_StubTask(task), cards)
        # берём только первые MAX_FOLLOWUP_RUBRICS рубрик и по RUBRIC_PAGES страниц
        assert queued == MAX_FOLLOWUP_RUBRICS * RUBRIC_PAGES

    def test_similar_rubric_names_not_duplicated(self, runner):
        # org_key отбрасывает номера филиалов, поэтому «Аптека 1» и «Аптека 2» —
        # одна рубрика, и второй запрос в очередь не попадёт
        task = self._generic_task(runner)
        cards = [make_card("Дентал", "Стоматология 1"), make_card("Дентал 2", "Стоматология 2")]
        queued = runner._plan_followups(_StubTask(task), cards)
        assert queued == RUBRIC_PAGES

    def test_task_without_cell_is_skipped(self, runner):
        task = {"payload": {"text": "Борисов", "cell": None}}
        assert runner._plan_followups(_StubTask(task), [make_card("Дентал")]) == 0

    def test_cards_without_category_skipped(self, runner):
        task = self._generic_task(runner)
        card = make_card("Дентал")
        card.categories = []
        card.category = ""
        assert runner._plan_followups(_StubTask(task), [card]) == 0


class _StubTask:
    """Задание-обёртка: планировщику нужен только payload."""

    def __init__(self, task: dict) -> None:
        self.payload = task["payload"]
        self.id = task.get("id")
        self.kind = task.get("kind", TaskKind.GENERIC.value)


class TestEstimateRequests:
    def test_estimate_accounts_for_queries_and_pages(self, runner):
        estimate = runner.estimate_requests()
        generic = estimate["общих запросов"]
        assert generic == len(runner.cells()) * len(GENERIC_QUERIES) * PAGES_PER_QUERY

    def test_rubrics_add_work(self, runner):
        assert (
            runner.estimate_requests(include_rubrics=False)["всего запросов"]
            < runner.estimate_requests(include_rubrics=True)["всего запросов"]
        )

    def test_estimate_is_positive(self, runner):
        assert runner.estimate_requests()["всего запросов"] > 0


class TestSkipLaterPages:
    """Пустая страница выдачи означает, что дальше идти незачем."""

    def _plan(self, runner) -> list[dict]:
        runner.plan_grid()
        return fetch_tasks(runner.db)

    def test_closes_only_later_pages(self, runner):
        tasks = self._plan(runner)
        first = next(task for task in tasks if task["payload"]["page"] == 1)
        skipped = runner.db.skip_later_pages(
            SOURCE, TaskKind.GENERIC.value, first["payload"], from_page=1
        )
        assert skipped == PAGES_PER_QUERY - 1
        left = [t for t in fetch_tasks(runner.db) if t["status"] == TaskStatus.PENDING.value]
        # у этого запроса в этой ячейке осталась только первая страница
        same = [
            t
            for t in left
            if t["payload"]["cell"] == first["payload"]["cell"]
            and t["payload"]["text"] == first["payload"]["text"]
        ]
        assert [t["payload"]["page"] for t in same] == [1]
        # а хвост второго запроса той же ячейки на месте
        other = [
            t
            for t in left
            if t["payload"]["cell"] == first["payload"]["cell"]
            and t["payload"]["text"] != first["payload"]["text"]
        ]
        assert sorted(t["payload"]["page"] for t in other) == list(
            range(1, PAGES_PER_QUERY + 1)
        )

    def test_other_cells_untouched(self, runner):
        tasks = self._plan(runner)
        first = next(task for task in tasks if task["payload"]["page"] == 1)
        runner.db.skip_later_pages(SOURCE, TaskKind.GENERIC.value, first["payload"], 1)
        others = [
            task
            for task in fetch_tasks(runner.db)
            if task["payload"]["cell"] != first["payload"]["cell"]
        ]
        assert all(task["status"] == TaskStatus.PENDING.value for task in others)

    def test_rubric_and_generic_not_mixed(self, runner):
        runner.plan_grid()
        runner.plan_rubrics(["аптека"])
        rubric = next(t for t in fetch_tasks(runner.db) if t["kind"] == TaskKind.RUBRIC.value)
        runner.db.skip_later_pages(SOURCE, TaskKind.RUBRIC.value, rubric["payload"], 1)
        # общие задания этой же ячейки не тронуты
        generic_same_cell = [
            t
            for t in fetch_tasks(runner.db)
            if t["kind"] == TaskKind.GENERIC.value
            and t["payload"]["cell"] == rubric["payload"]["cell"]
        ]
        assert generic_same_cell
        assert all(t["status"] == TaskStatus.PENDING.value for t in generic_same_cell)

    def test_page_two_closes_only_third(self, runner):
        tasks = self._plan(runner)
        second = next(t for t in tasks if t["payload"]["page"] == 2)
        skipped = runner.db.skip_later_pages(SOURCE, TaskKind.GENERIC.value, second["payload"], 2)
        assert skipped == PAGES_PER_QUERY - 2


class TestStorageIntegration:
    """Прогон должен сохранять организации и не плодить дубли."""

    def test_same_card_saved_once(self, runner):
        card = make_card("Дентал")
        assert runner.db.upsert_org(card) == "new"
        assert runner.db.upsert_org(card) == "updated"
        assert runner.db.count_orgs(SOURCE) == 1

    def test_same_org_from_two_cells_merged(self, runner):
        runner.db.upsert_org(make_card("Дентал"))
        other = make_card("Дентал")
        other.external_id = "other-id"
        other.lat, other.lon = 54.23, 28.49
        runner.db.upsert_org(other)
        rows = runner.db.org_rows(SOURCE)
        assert len(rows) == 2  # разные карточки, но одна группа при склейке
        assert runner.db.count_groups(SOURCE) == 1


class TestSearchPage:
    def test_keeps_text_and_page(self):
        page = SearchPage(text="аптека", bbox=None, page=2, payload={}, from_cache=False)
        assert page.page == 2
        assert page.text == "аптека"
        assert page.from_cache is False
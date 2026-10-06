"""Тесты склейки филиалов и выгрузки CSV."""

from ymcontacts.export import export_csv, export_summary
from ymcontacts.merge import merge_cards


def row(external_id: str, name: str, group_key: str, **kwargs) -> dict:
    """Карточка из базы в том виде, который отдаёт storage.orgs_with_contacts()."""
    base = {
        "external_id": external_id,
        "name": name,
        "group_key": group_key,
        "categories": [],
        "address": "",
        "phones": [],
        "website": "",
        "socials": {},
        "emails": [],
        "messengers": {},
        "lat": None,
        "lon": None,
        "url": "",
        "last_seen": "2026-01-01T10:00:00",
    }
    return {**base, **kwargs}


class TestMerge:
    def test_branches_merged(self):
        """Две карточки одной организации — одна строка с двумя адресами."""
        merged = merge_cards([
            row("1", "Суши Wok", "суши вок", address="ул. Ленина, 1", phones=["+375291111111"],
                lat=54.22, lon=28.48),
            row("2", "Суши Wok", "суши вок", address="ул. Победы, 5", phones=["+375292222222"],
                lat=54.23, lon=28.49),
        ])
        assert len(merged) == 1
        org = merged[0]
        assert org.branches == 2
        assert org.addresses == ["ул. Ленина, 1", "ул. Победы, 5"]
        assert org.phones == ["+375291111111", "+375292222222"]
        assert len(org.coords) == 2

    def test_different_orgs_not_merged(self):
        merged = merge_cards([
            row("1", "Пицца Хат", "пицца хат", phones=["+375291111111"]),
            row("2", "Суши Вок", "суши вок", phones=["+375292222222"]),
        ])
        assert len(merged) == 2

    def test_contacts_merged(self):
        """Почты и соцсети филиалов объединяются без дублей."""
        merged = merge_cards([
            row("1", "Кафе Уют", "кафе уют", emails=["a@uyut.by"], socials={"vk": "https://vk.com/1"}),
            row("2", "Кафе Уют", "кафе уют", emails=["a@uyut.by", "b@uyut.by"],
                socials={"vk": "https://vk.com/2"}),
        ])
        org = merged[0]
        assert org.emails == ["a@uyut.by", "b@uyut.by"]
        assert org.socials["vk"] == ["https://vk.com/1", "https://vk.com/2"]
        assert org.with_email is True

    def test_flags(self):
        merged = merge_cards([row("1", "Без контактов", "без контактов")])
        assert merged[0].with_phone is False
        assert merged[0].with_email is False


class TestExport:
    def test_csv_written(self, tmp_path):
        merged = merge_cards([
            row("1", "Аптека", "аптека", phones=["+375291111111"], address="ул. Ленина, 1",
                emails=["info@apteka.by"], website="https://apteka.by"),
        ])
        path = export_csv(merged, tmp_path / "out.csv")
        content = path.read_text(encoding="utf-8-sig")
        assert "Название" in content
        assert "Аптека" in content
        assert "+375291111111" in content
        assert ";" in content  # разделитель для русского Excel

    def test_only_with_contacts(self, tmp_path):
        merged = merge_cards([
            row("1", "С контактом", "с контактом", phones=["+375291111111"]),
            row("2", "Без контактов", "без контактов"),
        ])
        path = export_csv(merged, tmp_path / "out.csv", only_with_contacts=True)
        content = path.read_text(encoding="utf-8-sig")
        assert "С контактом" in content
        assert "Без контактов" not in content

    def test_summary(self):
        merged = merge_cards([
            row("1", "А", "а", phones=["+375291111111"]),
            row("2", "Б", "б"),
        ])
        summary = export_summary(merged)
        assert summary["организаций"] == 2
        assert summary["с телефоном"] == 1
        assert summary["% с телефоном"] == 50.0
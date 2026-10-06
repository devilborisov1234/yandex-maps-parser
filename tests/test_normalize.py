"""Тесты нормализации: телефоны, почты, ссылки, ключи склейки."""

from ymcontacts.normalize import (
    clean_url,
    extract_emails,
    extract_messengers,
    normalize_phone,
    org_key,
)


class TestPhones:
    def test_national_with_trunk_prefix(self):
        """Беларуский номер с восьмёркой: 8 (017) 33-12-45 -> +37517331245"""
        assert normalize_phone("8 (017) 33-12-45") == "+37517331245"

    def test_mobile_with_eight(self):
        """Мобильный с восьмёркой: 8 029 123-45-67 -> +375291234567"""
        assert normalize_phone("8 029 123-45-67") == "+375291234567"

    def test_international_already_correct(self):
        """Уже в E.164 — не трогаем."""
        assert normalize_phone("+375 29 123-45-67") == "+375291234567"

    def test_russian_number(self):
        """Российский номер с восьмёркой: 8 (495) 123-45-67 -> +74951234567"""
        assert normalize_phone("8 (495) 123-45-67") == "+74951234567"

    def test_garbage_returns_none(self):
        """Мусор не должен попадать в базу."""
        assert normalize_phone("нет телефона") is None
        assert normalize_phone("") is None
        assert normalize_phone(None) is None

    def test_short_number_rejected(self):
        """Слишком короткий номер — не телефон."""
        assert normalize_phone("123") is None


class TestEmails:
    def test_plain_email(self):
        assert extract_emails("пишите на info@borisov.by") == ["info@borisov.by"]

    def test_uppercase_normalized(self):
        assert extract_emails("Info@Borisov.BY") == ["info@borisov.by"]

    def test_image_in_srcset_is_not_email(self):
        """Хвост вида logo@2x.png в srcset — это не почта."""
        assert extract_emails('srcset="logo@2x.png"') == []

    def test_service_mails_filtered(self):
        assert extract_emails("noreply@example.org info@shop.by") == ["info@shop.by"]

    def test_deduplication(self):
        """Повторы схлопываются, порядок сохраняется."""
        assert extract_emails("a@shop.by b@shop.by a@shop.by") == ["a@shop.by", "b@shop.by"]


class TestUrls:
    def test_protocol_added(self):
        assert clean_url("shop.by/contacts") == "https://shop.by/contacts"

    def test_utm_stripped(self):
        assert clean_url("https://shop.by/contacts?utm_source=yandex") == "https://shop.by/contacts"

    def test_empty(self):
        assert clean_url("") == ""
        assert clean_url(None) == ""


class TestMessengers:
    def test_telegram(self):
        found = extract_messengers('ссылка <a href="https://t.me/borisov_shop">мы</a>')
        assert "telegram" in found
        assert "t.me/borisov_shop" in found["telegram"]

    def test_whatsapp_and_vk(self):
        html = 'wa.me/375291234567 и vk.com/borisov_shop'
        found = extract_messengers(html)
        assert "whatsapp" in found
        assert "vk" in found

    def test_nothing_found(self):
        assert extract_messengers("обычный текст без ссылок") == {}


class TestOrgKey:
    def test_same_name_different_case_and_quotes(self):
        """Регистр и кавычки не должны мешать склейке."""
        assert org_key('Суши "Wok"') == org_key("суши wok")

    def test_legal_forms_removed(self):
        """Юрлицо и его торговая марка должны склеиться."""
        assert org_key('ООО "Ромашка"') == org_key('"Ромашка"')

    def test_different_names_different_keys(self):
        assert org_key("Суши Wok") != org_key("Пицца Хат")

    def test_branch_number_noise(self):
        """Номер в названии — шум, ключ должен совпасть с базовым названием."""
        assert org_key("Суши Wok №5") == org_key("Суши Wok")
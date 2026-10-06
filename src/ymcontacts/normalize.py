"""Нормализация данных.

Идея: источник отдаёт данные в разном виде ("8 (017) 33-12-45", "+375 29 123-45-67",
"<a href='mailto:A@B.by'>"), а нам нужен один канонический вид. Всё приведение
сосредоточено здесь, чтобы остальной код не занимался регулярками.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import phonenumbers

# Код страны для разбора национальных номеров. Нужна, чтобы понимать,
# что "8 (017) 33-12-45" — это +375..., а не неизвестный номер.
COUNTRY_CODE = {
    "BY": "375", "RU": "7", "KZ": "7", "UA": "380",
}

# Допустимые регионы для разбора телефонов. Для Беларуси — BY, для РФ — RU.
DEFAULT_REGION = "BY"

# Телефон без разделителей: цифры и возможный плюс.
_PHONE_CHARS = re.compile(r"[^\d+]")
# Похоже на телефон: 7–15 цифр с разделителями.
_PHONE_CANDIDATE = re.compile(r"\+?[\d\s()\-]{7,20}")

# --- регулярки для почт и мессенджеров -------------------------------------
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")

# Расширения файлов: "ivan@company.png" — это не почта, это картинка в srcset
_BAD_EMAIL_SUFFIX = {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp", "tiff",
    "css", "js", "json", "xml", "woff", "woff2", "ttf", "otf",
}

# Служебные ящики, которые бесполезны для рассылки
_JUNK_EMAIL_LOCAL = {
    "noreply", "no-reply", "donotreply", "postmaster", "abuse", "hostmaster",
    "webmaster", "root", "info@example", "example",
}

# Домены, отдающие почту в виде счётчика/технического письма — не трогаем
_JUNK_EMAIL_DOMAINS = {
    "sentry.io", "wixpress.com", "email.com", "domain.com", "yourdomain.com",
    "site.com", "company.com", "mail.ru-placeholder",
}

_MESSENGER_PATTERNS: dict[str, re.Pattern] = {
    "telegram": re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(?!share|s/)([A-Za-z0-9_+\-]{4,40})", re.I),
    "whatsapp": re.compile(r"(?:https?://)?wa\.me/(\+?\d{8,15})", re.I),
    # Два варианта ссылки на Viber: приложение (viber://) и веб (chat.viber.com)
    "viber": re.compile(
        r"(?:viber://(?:chat\?number=)?(?:%2B|\+)?\d{8,15}"
        r"|(?:https?://)?chat\.viber\.com/(?:chat/)?\d{8,15})",
        re.I,
    ),
    "vk": re.compile(r"(?:https?://)?vk\.com/(?!share|photo|video|wall)([A-Za-z0-9_\-.]{3,40})", re.I),
    "ok": re.compile(r"(?:https?://)?ok\.ru/(?!share|dk|st)\w{3,40}", re.I),
    "instagram": re.compile(r"(?:https?://)?(?:instagram\.com|inst\.telegram)/([A-Za-z0-9_\-.]{3,40})", re.I),
    "facebook": re.compile(r"(?:https?://)?(?:facebook\.com|fb\.com)/([A-Za-z0-9\-.]{3,60})", re.I),
}

# Юридические формы и мусор из названий — вырезаем при построении ключа склейки
_LEGAL_FORMS = re.compile(
    r"\b(?:ооо|оао|зао|пао|ао|ип|чуп|гбу|мбу|нпо|нпп|торговый\s+дом|тд|фирма|компания|"
    r"общество\s+с\s+ограниченной\s+ответственностью|индивидуальный\s+предприниматель|"
    r"государственное\s+учреждение|унитарное\s+предприятие)\b",
    re.I,
)

_NON_WORD = re.compile(r"[^a-zа-яё0-9]+", re.I)
_QUOTES = re.compile(r"[«»\"'`„“”‘’()\[\]]")

# Слова-шум в названии, которые не различают филиалы
_BRANCH_NOISE = re.compile(
    r"\b(?:центр|магазин|салон|офис|магазин|отделение|филиал|пункт|выдача|"
    r"№\s*\d+|\d+)\b",
    re.I,
)


# --------------------------------------------------------------------------
# Телефоны
# --------------------------------------------------------------------------
def _e164_candidates(digits: str, region: str) -> list[str]:
    """Варианты номера в E.164 — от наиболее вероятного к остальным.

    Зачем варианты: строка "8 (495) 123-45-67" без указания страны
    неоднозначна. Восьмёрка бывает и в Беларуси, и в России, и отбросить
    её, не зная код страны, нельзя. Поэтому строим варианты с разными
    кодами стран, а выбирает normalize_phone — по реальной валидности
    номера в базе phonenumbers.
    """
    # Регион из настроек идёт первым, остальные коды — как запасные варианты.
    codes = [COUNTRY_CODE.get(region.upper(), "7")]
    codes += [code for code in ("375", "7", "380") if code not in codes]

    candidates: list[str] = []
    for code in codes:
        rest = digits
        # Номер уже содержит код страны — не приклеиваем второй раз
        if rest.startswith(code) and len(rest) - len(code) >= 6:
            pass
        # Национальный формат: убираем восьмёрку и ведущий ноль
        elif rest.startswith(("8", "0")):
            rest = rest.lstrip("80") or rest
            candidates.append(f"+{code}{rest}")
            continue
        else:
            rest = code + rest
        if 9 <= len(rest) <= 15:
            candidates.append(f"+{rest}")
    return candidates


def normalize_phone(raw: str | None, region: str = DEFAULT_REGION) -> str | None:
    """Привести телефон к E.164 (+375291234567). None — если разобрать не удалось."""
    if not raw:
        return None
    cleaned = _PHONE_CHARS.sub("", raw)
    if not cleaned or not cleaned.lstrip("+").isdigit():
        return None

    # Номер уже в международном формате — доверяем как есть
    candidates = [cleaned] if cleaned.startswith("+") else _e164_candidates(cleaned, region)

    possible: str | None = None
    for candidate in candidates:
        try:
            parsed = phonenumbers.parse(candidate, region)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        # Правдоподобный номер (например, нестандартный 800-номер) — пригодится
        if possible is None and phonenumbers.is_possible_number(parsed):
            possible = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    return possible


def extract_phones(text: str, region: str = DEFAULT_REGION) -> list[str]:
    """Вытащить все телефоны из произвольного текста, вернуть уникальные в E.164."""
    if not text:
        return []
    found: list[str] = []
    for candidate in _PHONE_CANDIDATE.findall(text):
        phone = normalize_phone(candidate, region)
        if phone and phone not in found:
            found.append(phone)
    return found


# --------------------------------------------------------------------------
# Почта
# --------------------------------------------------------------------------
def normalize_email(raw: str) -> str | None:
    """Привести адрес к нижнему регистру, отбросить мусор. None — если это не почта."""
    if not raw:
        return None
    email = raw.strip().strip(".,;:()<>[]\"'").lower()
    if not _EMAIL_RE.fullmatch(email):
        return None
    local, _, domain = email.partition("@")
    if not local or "." not in domain:
        return None
    if domain.rsplit(".", 1)[-1] in _BAD_EMAIL_SUFFIX:
        return None
    if domain in _JUNK_EMAIL_DOMAINS or local in _JUNK_EMAIL_LOCAL:
        return None
    return email


def extract_emails(text: str) -> list[str]:
    """Вытащить уникальные почты из текста HTML или обычного текста."""
    if not text:
        return []
    result: list[str] = []
    for raw in _EMAIL_RE.findall(text):
        email = normalize_email(raw)
        if email and email not in result:
            result.append(email)
    return result


# --------------------------------------------------------------------------
# Ссылки
# --------------------------------------------------------------------------
def clean_url(url: str | None) -> str:
    """Привести URL к виду https://site.by/path без мусора и без схемы в виде '//'."""
    if not url:
        return ""
    url = url.strip().replace("\\/", "/")
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        return ""
    # Убираем utm-хвост: он не нужен для обхода и засоряет данные.
    clean = url.split("?", 1)[0]
    clean = clean.split("#", 1)[0]
    return clean.rstrip("/")


def domain_of(url: str) -> str:
    """Домен без www — для отчётов и отладки."""
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return ""


def extract_messengers(text: str) -> dict[str, str]:
    """Найти в HTML/тексте ссылки на мессенджеры и соцсети.

    Возвращает {"telegram": "https://t.me/...", "whatsapp": "..."} и т.п.
    """
    if not text:
        return {}
    found: dict[str, str] = {}
    for name, pattern in _MESSENGER_PATTERNS.items():
        if name in found:
            continue
        match = pattern.search(text)
        if match:
            found[name] = clean_url(match.group(0)) or match.group(0)
    return found


# --------------------------------------------------------------------------
# Склейка филиалов
# --------------------------------------------------------------------------
def org_key(name: str) -> str:
    """Ключ для склейки карточек одной организации.

    "Суши Wok" на Немига и Суше на Победы должны получить один ключ,
    а "Суши Wok №2" — другой (разные точки — но в одном key мы их склеим как
    филиалы; если нужно различать, см. _BRANCH_NOISE ниже).
    """
    if not name:
        return ""
    text = name.lower().replace("ё", "е")
    text = _QUOTES.sub(" ", text)
    text = _LEGAL_FORMS.sub(" ", text)
    text = _BRANCH_NOISE.sub(" ", text)
    text = _NON_WORD.sub(" ", text)
    return " ".join(text.split()).strip()
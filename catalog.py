"""Жёстко заданный справочник подписок: цены не меняются ни организаторами, ни базой."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Service:
    slug: str
    title: str
    total_price: int  # общая стоимость подписки, ₸ в месяц
    price_per_slot: int  # доля одного участника, ₸ в месяц
    max_slots: int  # всего мест в группе, включая организатора


CATALOG: tuple[Service, ...] = (
    Service("yandex_plus", "Яндекс Плюс", 3400, 850, 4),
    Service("youtube_premium", "YouTube Premium", 5400, 900, 6),
    Service("chatgpt_team", "ChatGPT Team", 25000, 6250, 4),
    Service("google_one", "Google One (2 ТБ)", 4800, 800, 6),
)
BY_SLUG = {s.slug: s for s in CATALOG}


def get_service(slug: str) -> Service | None:
    return BY_SLUG.get(slug)


def title_of(slug: str) -> str:
    """Название сервиса; для slug не из справочника (старые записи) показываем сам slug."""
    service = BY_SLUG.get(slug)
    return service.title if service else slug


def money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " ₸"

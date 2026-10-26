from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bot.db.models import Group, GroupStatus, Membership, MembershipStatus, Service, User


async def test_create_full_chain(session_maker) -> None:
    async with session_maker() as s:
        owner = User(telegram_id=1, username="owner")
        member = User(telegram_id=2, username=None)
        service = Service(
            name="Spotify", plan_name="Family", price_month=Decimal("299"), max_members=6
        )
        s.add_all([owner, member, service])
        await s.flush()
        group = Group(
            service_id=service.id,
            owner_id=owner.id,
            price_per_member=Decimal("50"),
            slots_total=5,
            slots_free=5,
            billing_day=10,
        )
        s.add(group)
        await s.flush()
        s.add(Membership(group_id=group.id, user_id=member.id))
        await s.commit()

    async with session_maker() as s:
        group = await s.scalar(select(Group))
        membership = await s.scalar(select(Membership))
        user = await s.scalar(select(User).where(User.telegram_id == 1))
    assert group.status == GroupStatus.OPEN
    assert membership.status == MembershipStatus.PENDING
    assert user.language == "ru" and user.is_banned is False
    assert user.created_at is not None


async def test_telegram_id_unique(session_maker) -> None:
    async with session_maker() as s:
        s.add_all([User(telegram_id=1), User(telegram_id=1)])
        with pytest.raises(IntegrityError):
            await s.commit()

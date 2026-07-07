from itertools import count

from django.contrib.auth.models import Permission, User
from eveuniverse.models import EveType

from allianceauth.eveonline.models import EveCharacter

from fitcheck.models import Doctrine, DoctrineFit, DoctrineFitItem
from fitcheck.models.doctrine import SubstitutionPolicy

_character_ids = count(90_000_001)


def create_user(username="pilot", permissions=("basic_access",)) -> User:
    user = User.objects.create_user(username=username, password="password")
    for codename in permissions:
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="fitcheck", codename=codename
            )
        )
    # AA decorates hooked URLs with main_character_required.
    character = EveCharacter.objects.create(
        character_id=next(_character_ids),
        character_name=f"Pilot {username}",
        corporation_id=2001,
        corporation_name="Test Corp",
        corporation_ticker="TEST",
        security_status=0,
    )
    user.profile.main_character = character
    user.profile.save()
    return User.objects.get(pk=user.pk)  # refresh permission cache


def create_doctrine(name="Alliance Armor", **kwargs) -> Doctrine:
    return Doctrine.objects.create(name=name, **kwargs)


def create_fit(
    doctrine: Doctrine | None,
    ship_type_id: int,
    name="Test Fit",
    **kwargs,
) -> DoctrineFit:
    fit = DoctrineFit.objects.create(
        name=name,
        ship_type=EveType.objects.get(id=ship_type_id),
        eft_source="",
        **kwargs,
    )
    if doctrine is not None:
        fit.doctrines.add(doctrine)
    return fit


def add_item(
    fit: DoctrineFit,
    section: str,
    type_id: int,
    quantity: int = 1,
    *,
    policy: str | None = None,
    charge_type_id: int | None = None,
    **kwargs,
) -> DoctrineFitItem:
    return DoctrineFitItem.objects.create(
        fit=fit,
        section=section,
        module_type=EveType.objects.get(id=type_id),
        quantity=quantity,
        charge_type=EveType.objects.get(id=charge_type_id) if charge_type_id else None,
        policy=policy or SubstitutionPolicy.VARIANTS,
        **kwargs,
    )

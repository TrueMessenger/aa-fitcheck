"""Tests for authoritative colcrunch category sync.

colcrunch's `Category` carries a `groups` access-restriction M2M plus M2M to
doctrines + fittings. Importing / re-syncing mirrors those into our
`DoctrineCategory` (the visibility object), with the fittings plugin as the
source of truth: colour, group-gating and membership are overwritten each run.
Purely-local categories (no source_plugin_pk) are never touched.

The plugin isn't installed in the test env, so the colcrunch models are
hand-rolled duck-typed fakes matching the interface the sync code reads
(pk, name, color, .groups/.doctrines/.fittings.all(), and the `category` M2M
on plugin doctrines/fittings).
"""

from unittest import mock

from django.contrib.auth.models import Group
from django.test import TestCase

from ..models import Doctrine, DoctrineCategory, DoctrineFit
from ..services.fittings_import import (
    import_plugin_doctrines,
    resync_doctrine_from_plugin,
)
from .testdata.factories import create_user
from .testdata.sde_fixtures import T, create_sde_testdata


# ---------- duck-typed plugin doubles -----------------------------------------


class FakeRelated:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)


class FakeItem:
    def __init__(self, type_id, flag, quantity=1):
        self.type_id = type_id
        self.flag = flag
        self.quantity = quantity


class FakeCategory:
    def __init__(self, pk, name, color="#112233", groups=(), doctrines=(), fittings=()):
        self.pk = pk
        self.name = name
        self.color = color
        self.groups = FakeRelated(groups)
        self.doctrines = FakeRelated(doctrines)
        self.fittings = FakeRelated(fittings)


class FakeFitting:
    def __init__(self, pk, name, ship_type_id, items, categories=()):
        self.pk = pk
        self.name = name
        self.ship_type_type_id = ship_type_id
        self.items = FakeRelated(items)
        self.category = FakeRelated(categories)


class FakeDoctrine:
    def __init__(self, pk, name, fittings, description="", categories=()):
        self.pk = pk
        self.name = name
        self.description = description
        self.fittings = FakeRelated(fittings)
        self.category = FakeRelated(categories)


class FakeDoctrineManager:
    def __init__(self, doctrines):
        self._by_pk = {d.pk: d for d in doctrines}

    def filter(self, **kwargs):
        result = list(self._by_pk.values())
        if "pk__in" in kwargs:
            wanted = set(kwargs["pk__in"])
            result = [d for d in result if d.pk in wanted]
        if "pk" in kwargs:
            result = [d for d in result if d.pk == kwargs["pk"]]
        return _FakeQuerySet(result)


class _FakeQuerySet(list):
    def prefetch_related(self, *_args, **_kwargs):
        return self

    def first(self):
        return self[0] if self else None


def _patched_fittings(doctrines):
    """fittings_installed -> True, _plugin_models -> fakes, and a non-None
    _plugin_category_model so category sync runs."""

    class PluginDoctrineCls:
        objects = FakeDoctrineManager(doctrines)

    class PluginFittingCls:
        pass

    return mock.patch.multiple(
        "fitcheck.services.fittings_import",
        fittings_installed=lambda: True,
        _plugin_models=lambda: (PluginDoctrineCls, PluginFittingCls),
        _plugin_category_model=lambda: object,
    )


def _harb_fit(pk=501, name="Plugin Harb", categories=()):
    return FakeFitting(
        pk=pk,
        name=name,
        ship_type_id=T.HARBINGER,
        items=[
            FakeItem(T.HEAT_SINK_II, "LoSlot0"),
            FakeItem(T.HEAT_SINK_II, "LoSlot1"),
            FakeItem(T.HEAT_SINK_II, "LoSlot2"),
        ],
        categories=categories,
    )


class TestCategorySyncOnImport(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        self.user = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        self.cap_group = Group.objects.create(name="Capitals")

    def _wire(self, color="#ff0000", groups=None):
        cat = FakeCategory(pk=9, name="Capitals", color=color,
                           groups=groups if groups is not None else [self.cap_group])
        fit = _harb_fit(categories=[cat])
        doctrine = FakeDoctrine(pk=42, name="Plugin Armor", fittings=[fit], categories=[cat])
        cat.doctrines = FakeRelated([doctrine])
        cat.fittings = FakeRelated([fit])
        return doctrine, fit, cat

    def test_import_creates_category_with_groups_and_membership(self):
        plugin_doctrine, _fit, _cat = self._wire()
        with _patched_fittings([plugin_doctrine]):
            report = import_plugin_doctrines(self.user, [42])

        self.assertEqual(report.categories_synced, ["Capitals"])
        category = DoctrineCategory.objects.get(name="Capitals")
        self.assertEqual(category.source_plugin_pk, 9)
        self.assertEqual(category.color, "#ff0000")
        # colcrunch groups -> Selected-OR; Required left empty.
        self.assertEqual(
            list(category.selected_groups.values_list("name", flat=True)), ["Capitals"]
        )
        self.assertFalse(category.required_groups.exists())
        # Membership points at the doctrine + fit we just imported.
        our_doctrine = Doctrine.objects.get(source_plugin_pk=42)
        our_fit = DoctrineFit.objects.get(source_plugin_pk=501)
        self.assertIn(our_doctrine, category.doctrines.all())
        self.assertIn(our_fit, category.fits.all())

    def test_color_falls_back_when_not_hex(self):
        plugin_doctrine, _fit, _cat = self._wire(color="not-a-color")
        with _patched_fittings([plugin_doctrine]):
            import_plugin_doctrines(self.user, [42])
        category = DoctrineCategory.objects.get(name="Capitals")
        self.assertEqual(category.color, "#0d6efd")


class TestCategorySyncIsAuthoritative(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        self.user = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        self.group_a = Group.objects.create(name="GroupA")
        self.group_b = Group.objects.create(name="GroupB")

    def _doctrine_with_category(self, groups):
        cat = FakeCategory(pk=9, name="Strategic", groups=groups)
        fit = _harb_fit(categories=[cat])
        doctrine = FakeDoctrine(pk=42, name="Plugin Armor", fittings=[fit], categories=[cat])
        cat.doctrines = FakeRelated([doctrine])
        cat.fittings = FakeRelated([fit])
        return doctrine

    def test_resync_overwrites_group_gating_and_clears_required(self):
        # Initial import with GroupA.
        with _patched_fittings([self._doctrine_with_category([self.group_a])]):
            import_plugin_doctrines(self.user, [42])
        category = DoctrineCategory.objects.get(source_plugin_pk=9)
        # Admin hand-edits gating: swap to a required group.
        category.selected_groups.clear()
        category.required_groups.set([self.group_a])
        doctrine = Doctrine.objects.get(source_plugin_pk=42)

        # colcrunch now says GroupB (Selected-OR). Resync is authoritative.
        with _patched_fittings([self._doctrine_with_category([self.group_b])]):
            report = resync_doctrine_from_plugin(doctrine, self.user)

        self.assertIn("Strategic", report.categories_synced)
        category.refresh_from_db()
        self.assertEqual(
            list(category.selected_groups.values_list("name", flat=True)), ["GroupB"]
        )
        self.assertFalse(category.required_groups.exists())


class TestLocalCategoriesUntouched(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        self.user = create_user("mgr", permissions=("basic_access", "manage_doctrines"))

    def test_local_only_category_is_not_modified(self):
        local_group = Group.objects.create(name="LocalGroup")
        local = DoctrineCategory.objects.create(name="Hand Made", color="#abcdef")
        local.required_groups.set([local_group])

        cap = Group.objects.create(name="Capitals")
        cat = FakeCategory(pk=9, name="Capitals", groups=[cap])
        fit = _harb_fit(categories=[cat])
        plugin_doctrine = FakeDoctrine(pk=42, name="Plugin Armor", fittings=[fit], categories=[cat])
        cat.doctrines = FakeRelated([plugin_doctrine])
        cat.fittings = FakeRelated([fit])

        with _patched_fittings([plugin_doctrine]):
            import_plugin_doctrines(self.user, [42])

        local.refresh_from_db()
        self.assertIsNone(local.source_plugin_pk)
        self.assertEqual(local.color, "#abcdef")
        self.assertEqual(
            list(local.required_groups.values_list("name", flat=True)), ["LocalGroup"]
        )
        self.assertFalse(local.selected_groups.exists())

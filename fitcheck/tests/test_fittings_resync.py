"""Tests for the colcrunch fittings re-sync flow.

The plugin itself isn't installed in the test env, so `fittings_installed`
and `_plugin_models` are patched with hand-rolled duck-typed fakes that
match the interface our converter expects (pk, name, .fittings.all(),
.items.all() with .flag/.type_id/.quantity).
"""

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from ..constants import Section
from ..models import DoctrineFit, FitItemOverride
from ..services.fittings_import import (
    ImportReport,
    import_plugin_doctrines,
    resync_doctrine_from_plugin,
)
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata


# ---------- duck-typed plugin doubles -----------------------------------------


class FakeItem:
    def __init__(self, type_id, flag, quantity=1):
        self.type_id = type_id
        self.flag = flag
        self.quantity = quantity


class FakeRelated:
    """Mimics a Django RelatedManager: .all() returns the stashed list."""

    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)


class FakeFitting:
    def __init__(self, pk, name, ship_type_id, items):
        self.pk = pk
        self.name = name
        self.ship_type_type_id = ship_type_id
        self.items = FakeRelated(items)


class FakeDoctrine:
    def __init__(self, pk, name, fittings, description=""):
        self.pk = pk
        self.name = name
        self.description = description
        self.fittings = FakeRelated(fittings)


class FakeDoctrineManager:
    """Mimics the DoctrineModel.objects queryset shape that the import code
    uses: .filter(pk__in=[...]).prefetch_related(...) and .filter(pk=N).first()."""

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
    """Context-manager helper: makes fittings_installed return True and
    _plugin_models() return wrappers around our fake objects."""

    class PluginDoctrineCls:
        objects = FakeDoctrineManager(doctrines)

    class PluginFittingCls:
        pass

    return mock.patch.multiple(
        "fitcheck.services.fittings_import",
        fittings_installed=lambda: True,
        _plugin_models=lambda: (PluginDoctrineCls, PluginFittingCls),
    )


class TestImportStampsSourcePluginPk(TestCase):
    """Import path now stamps source_plugin_pk so re-sync can find the row again."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_imported_doctrine_and_fits_carry_source_pk(self):
        user = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        fake_fit = FakeFitting(
            pk=501, name="Plugin Harb", ship_type_id=T.HARBINGER,
            items=[FakeItem(T.HEAT_SINK_II, "LoSlot0"),
                   FakeItem(T.HEAT_SINK_II, "LoSlot1"),
                   FakeItem(T.HEAT_SINK_II, "LoSlot2")],
        )
        fake_doctrine = FakeDoctrine(pk=42, name="Plugin Armor", fittings=[fake_fit])

        with _patched_fittings([fake_doctrine]):
            report = import_plugin_doctrines(user, [42])

        self.assertEqual(report.doctrines_created, ["Plugin Armor"])
        self.assertEqual(report.fits_created, ["Plugin Harb"])

        from ..models import Doctrine

        doctrine = Doctrine.objects.get(name="Plugin Armor")
        self.assertEqual(doctrine.source_plugin_pk, 42)
        fit = DoctrineFit.objects.get(name="Plugin Harb")
        self.assertEqual(fit.source_plugin_pk, 501)


class TestResyncDoctrineFromPlugin(TestCase):
    """The resync flow refreshes BOMs but preserves our policy data."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def setUp(self):
        super().setUp()
        self.user = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        # Initial import: 3 Heat Sinks in Plugin Armor doctrine.
        v1_fit = FakeFitting(
            pk=501, name="Plugin Harb", ship_type_id=T.HARBINGER,
            items=[FakeItem(T.HEAT_SINK_II, "LoSlot0"),
                   FakeItem(T.HEAT_SINK_II, "LoSlot1"),
                   FakeItem(T.HEAT_SINK_II, "LoSlot2")],
        )
        self.v1_doctrine = FakeDoctrine(pk=42, name="Plugin Armor", fittings=[v1_fit])
        with _patched_fittings([self.v1_doctrine]):
            import_plugin_doctrines(self.user, [42])
        from ..models import Doctrine
        self.doctrine = Doctrine.objects.get(source_plugin_pk=42)
        self.fit = DoctrineFit.objects.get(source_plugin_pk=501)

    def test_no_changes_reports_unchanged(self):
        with _patched_fittings([self.v1_doctrine]):
            report = resync_doctrine_from_plugin(self.doctrine, self.user)
        self.assertEqual(report.unchanged, ["Plugin Harb"])
        self.assertFalse(report.changed_anything)

    def test_swapped_module_updates_bom_and_bumps_version(self):
        """Plugin admin changed the fit: 3 Heat Sinks -> 2 Heat Sinks + 1 Cap
        Recharger. Our re-sync replaces the items and bumps the version."""
        v1_version = self.fit.version
        v2_fit = FakeFitting(
            pk=501, name="Plugin Harb", ship_type_id=T.HARBINGER,
            items=[FakeItem(T.HEAT_SINK_II, "LoSlot0"),
                   FakeItem(T.HEAT_SINK_II, "LoSlot1"),
                   FakeItem(T.CAP_RECHARGER_II, "MedSlot0")],
        )
        v2_doctrine = FakeDoctrine(pk=42, name="Plugin Armor", fittings=[v2_fit])

        with _patched_fittings([v2_doctrine]):
            report = resync_doctrine_from_plugin(self.doctrine, self.user)

        self.assertEqual(report.fits_updated, ["Plugin Harb"])
        self.fit.refresh_from_db()
        self.assertGreater(self.fit.version, v1_version)
        sections = set(self.fit.items.values_list("section", "module_type_id"))
        self.assertEqual(
            sections,
            {(Section.LOW, T.HEAT_SINK_II), (Section.MED, T.CAP_RECHARGER_II)},
        )

    def test_resync_preserves_overrides_on_unchanged_module(self):
        """Admin tuned a FitItemOverride on the Heat Sink row. Re-syncing
        with the same BOM keeps the rule, and even with a partial change
        rules on surviving modules stick."""
        from eveuniverse.models import EveType

        hs_item = self.fit.items.get(module_type_id=T.HEAT_SINK_II)
        navy_hs = EveType.objects.get(id=T.HEAT_SINK_IMPERIAL)
        FitItemOverride.objects.create(
            item=hs_item, alt_type=navy_hs,
            mode=FitItemOverride.Mode.INCLUDE,
        )

        # Plugin changed quantity but kept Heat Sink as the low-slot type.
        v2_fit = FakeFitting(
            pk=501, name="Plugin Harb", ship_type_id=T.HARBINGER,
            items=[FakeItem(T.HEAT_SINK_II, "LoSlot0"),
                   FakeItem(T.HEAT_SINK_II, "LoSlot1"),
                   FakeItem(T.HEAT_SINK_II, "LoSlot2"),
                   FakeItem(T.HEAT_SINK_II, "LoSlot3")],
        )
        v2_doctrine = FakeDoctrine(pk=42, name="Plugin Armor", fittings=[v2_fit])
        with _patched_fittings([v2_doctrine]):
            resync_doctrine_from_plugin(self.doctrine, self.user)

        # New host item under the updated qty=4, override still in place.
        new_hs_item = self.fit.items.get(module_type_id=T.HEAT_SINK_II)
        self.assertEqual(new_hs_item.quantity, 4)
        self.assertTrue(
            new_hs_item.overrides.filter(alt_type_id=T.HEAT_SINK_IMPERIAL).exists()
        )

    def test_resync_preserves_per_item_policy_fields(self):
        """Re-sync now carries per-item policy (not just overrides) forward onto
        surviving modules - closing the old bug where it reset them to defaults."""
        from ..models.doctrine import SubstitutionPolicy

        hs_item = self.fit.items.get(module_type_id=T.HEAT_SINK_II)
        hs_item.policy = SubstitutionPolicy.MEET_OR_BEAT
        hs_item.checked_attributes = [64]
        hs_item.notes = "carry me"
        hs_item.save()

        v2_fit = FakeFitting(
            pk=501, name="Plugin Harb", ship_type_id=T.HARBINGER,
            items=[FakeItem(T.HEAT_SINK_II, "LoSlot0"),
                   FakeItem(T.HEAT_SINK_II, "LoSlot1"),
                   FakeItem(T.CAP_RECHARGER_II, "MedSlot0")],
        )
        v2_doctrine = FakeDoctrine(pk=42, name="Plugin Armor", fittings=[v2_fit])
        with _patched_fittings([v2_doctrine]):
            resync_doctrine_from_plugin(self.doctrine, self.user)

        new_hs = self.fit.items.get(module_type_id=T.HEAT_SINK_II)
        self.assertEqual(new_hs.policy, SubstitutionPolicy.MEET_OR_BEAT)
        self.assertEqual(new_hs.checked_attributes, [64])
        self.assertEqual(new_hs.notes, "carry me")

    def test_dropped_fit_detaches_but_keeps_fitting_standalone(self):
        """Plugin removed Plugin Harb from this doctrine. We detach it but
        leave the DoctrineFit alive as a standalone standard."""
        empty_doctrine = FakeDoctrine(pk=42, name="Plugin Armor", fittings=[])
        with _patched_fittings([empty_doctrine]):
            report = resync_doctrine_from_plugin(self.doctrine, self.user)

        self.assertEqual(report.fits_dropped, ["Plugin Harb"])
        # Fitting survives, just not in this doctrine.
        self.assertTrue(DoctrineFit.objects.filter(pk=self.fit.pk).exists())
        self.assertNotIn(self.doctrine, self.fit.doctrines.all())

    def test_new_plugin_fit_is_added(self):
        """Plugin added a new fit to the doctrine. We import it and link it."""
        v1_fit = FakeFitting(
            pk=501, name="Plugin Harb", ship_type_id=T.HARBINGER,
            items=[FakeItem(T.HEAT_SINK_II, "LoSlot0"),
                   FakeItem(T.HEAT_SINK_II, "LoSlot1"),
                   FakeItem(T.HEAT_SINK_II, "LoSlot2")],
        )
        new_fit = FakeFitting(
            pk=502, name="Plugin Oracle", ship_type_id=T.ORACLE,
            items=[FakeItem(T.HEAT_SINK_II, "LoSlot0")],
        )
        v2_doctrine = FakeDoctrine(pk=42, name="Plugin Armor", fittings=[v1_fit, new_fit])
        with _patched_fittings([v2_doctrine]):
            report = resync_doctrine_from_plugin(self.doctrine, self.user)

        self.assertEqual(report.fits_added, ["Plugin Oracle"])
        oracle = DoctrineFit.objects.get(name="Plugin Oracle")
        self.assertEqual(oracle.source_plugin_pk, 502)
        self.assertIn(self.doctrine, oracle.doctrines.all())


class TestResyncErrorPaths(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_doctrine_without_source_pk_reports_error(self):
        user = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        doctrine = create_doctrine("Local-only")  # no source_plugin_pk
        with mock.patch(
            "fitcheck.services.fittings_import.fittings_installed",
            return_value=True,
        ):
            report = resync_doctrine_from_plugin(doctrine, user)
        self.assertIn("not imported", report.error)

    def test_plugin_not_installed_reports_error(self):
        user = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        doctrine = create_doctrine("Was-imported")
        doctrine.source_plugin_pk = 42
        doctrine.save(update_fields=["source_plugin_pk"])
        with mock.patch(
            "fitcheck.services.fittings_import.fittings_installed",
            return_value=False,
        ):
            report = resync_doctrine_from_plugin(doctrine, user)
        self.assertIn("not installed", report.error)


class TestResyncView(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_view_403s_without_perm(self):
        member = create_user("member")
        doctrine = create_doctrine("X")
        doctrine.source_plugin_pk = 42
        doctrine.save(update_fields=["source_plugin_pk"])
        self.client.force_login(member)
        response = self.client.post(
            reverse("fitcheck:doctrine_resync_from_plugin", args=[doctrine.pk])
        )
        # @permission_required redirects to login for non-managers.
        self.assertEqual(response.status_code, 302)

    def test_view_flashes_when_not_imported(self):
        mgr = create_user("mgr", permissions=("basic_access", "manage_doctrines"))
        doctrine = create_doctrine("Local-only")  # no source pk
        self.client.force_login(mgr)
        response = self.client.post(
            reverse("fitcheck:doctrine_resync_from_plugin", args=[doctrine.pk]),
            follow=True,
        )
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("wasn't imported" in m for m in messages), messages)

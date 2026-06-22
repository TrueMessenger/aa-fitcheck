from django.test import TestCase
from django.urls import reverse

from ..constants import EveMetaGroupId, Section
from ..models import DoctrineFitItem, FitItemOverride
from ..models.doctrine import SubstitutionPolicy
from ..services.substitutions import possible_meta_groups_for_item
from .testdata.factories import add_item, create_doctrine, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata


class PolicyEditorTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()
        cls.doctrine = create_doctrine()
        cls.fit = create_fit(cls.doctrine, T.HARBINGER, name="Armor Brawl")
        cls.low_item = add_item(cls.fit, Section.LOW, T.HEAT_SINK_II, 3)
        cls.cargo_item = add_item(
            cls.fit, Section.CARGO, T.NITROGEN_ISOTOPES, 30000,
            policy=SubstitutionPolicy.EXACT,
        )
        cls.manager = create_user("manager", permissions=["basic_access", "manage_doctrines"])
        cls.member = create_user("member")

    def _formset_data(self, changes=None):
        changes = changes or {}
        items = list(
            DoctrineFitItem.objects.filter(fit=self.fit).order_by("pk")
        )
        data = {
            "form-TOTAL_FORMS": str(len(items)),
            "form-INITIAL_FORMS": str(len(items)),
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
        }
        for index, item in enumerate(items):
            prefix = f"form-{index}"
            # Mirror the browser: only the meta-group boxes actually rendered for
            # this item (its family's possible groups) can be submitted/checked.
            possible = possible_meta_groups_for_item(item)
            values = {
                "id": str(item.pk),
                "policy": item.policy,
                "allow_mutated": "on" if item.allow_mutated else "",
                "allowed_meta_groups": [
                    str(g) for g in (item.allowed_meta_groups or []) if g in possible
                ],
                "min_quantity_pct": str(item.min_quantity_pct),
                "notes": item.notes,
            }
            values.update(changes.get(item.pk, {}))
            for key, value in values.items():
                if key == "allow_mutated" and not value:
                    continue  # unchecked checkboxes are absent from POST data
                if key == "allowed_meta_groups":
                    data.setdefault(f"{prefix}-{key}", [])
                    data[f"{prefix}-{key}"] = value
                    continue
                data[f"{prefix}-{key}"] = value
        return data


class TestFitItemsEditor(PolicyEditorTestCase):
    def test_requires_manage_permission(self):
        url = reverse("fitcheck:manage_fit_items", args=[self.fit.pk])
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.manager)
        response = self.client.get(url)
        self.assertContains(response, "Heat Sink II")
        self.assertContains(response, "Nitrogen Isotopes")

    def test_save_policy_meta_groups_and_quantity_pct(self):
        self.client.force_login(self.manager)
        url = reverse("fitcheck:manage_fit_items", args=[self.fit.pk])
        old_version = self.fit.version
        data = self._formset_data(
            {
                self.low_item.pk: {
                    "policy": SubstitutionPolicy.MEET_OR_BEAT,
                    "allowed_meta_groups": [
                        str(EveMetaGroupId.TECH_II),
                        str(EveMetaGroupId.FACTION),
                    ],
                },
                self.cargo_item.pk: {"min_quantity_pct": "66"},
            }
        )
        response = self.client.post(url, data, follow=True)
        self.assertEqual(response.status_code, 200)

        self.low_item.refresh_from_db()
        self.assertEqual(self.low_item.policy, SubstitutionPolicy.MEET_OR_BEAT)
        self.assertEqual(
            sorted(self.low_item.allowed_meta_groups),
            [EveMetaGroupId.TECH_II, EveMetaGroupId.FACTION],
        )
        self.cargo_item.refresh_from_db()
        self.assertEqual(self.cargo_item.min_quantity_pct, 66)

        self.fit.refresh_from_db()
        self.assertEqual(self.fit.version, old_version + 1)

    def test_unchanged_save_does_not_bump_version(self):
        self.client.force_login(self.manager)
        url = reverse("fitcheck:manage_fit_items", args=[self.fit.pk])
        # Normalize stored meta groups to each item's possible set first (the
        # heal-on-save state). Only then is a subsequent editor save genuinely a
        # no-op; an un-normalized [1..6] default would heal+bump on first save.
        for item in DoctrineFitItem.objects.filter(fit=self.fit):
            item.allowed_meta_groups = sorted(possible_meta_groups_for_item(item))
            item.save(update_fields=["allowed_meta_groups"])
        old_version = self.fit.version
        self.client.post(url, self._formset_data(), follow=True)
        self.fit.refresh_from_db()
        self.assertEqual(self.fit.version, old_version)

    def test_editor_offers_only_possible_meta_groups(self):
        self.client.force_login(self.manager)
        url = reverse("fitcheck:manage_fit_items", args=[self.fit.pk])
        response = self.client.get(url)
        # The Heat Sink family has Faction variants, so that checkbox is offered;
        # no Officer/Deadspace heat sinks (nor isotopes) exist, so those labels
        # never render on any row.
        self.assertContains(response, "Faction")
        self.assertNotContains(response, "Officer")
        self.assertNotContains(response, "Deadspace")

    def test_post_rejects_impossible_meta_group(self):
        self.client.force_login(self.manager)
        url = reverse("fitcheck:manage_fit_items", args=[self.fit.pk])
        data = self._formset_data(
            {
                self.low_item.pk: {
                    "policy": SubstitutionPolicy.MEET_OR_BEAT,
                    "allowed_meta_groups": [
                        str(EveMetaGroupId.TECH_II),
                        str(EveMetaGroupId.OFFICER),  # impossible for a heat sink
                    ],
                }
            }
        )
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)  # invalid -> re-rendered
        self.assertContains(response, "valid choice")  # Officer rejected by choices
        self.low_item.refresh_from_db()
        # Nothing saved: the policy change is rejected because it carried an
        # impossible group, so the row stays at its untouched default.
        self.assertEqual(self.low_item.policy, SubstitutionPolicy.VARIANTS)
        self.assertEqual(sorted(self.low_item.allowed_meta_groups), [1, 2, 3, 4, 5, 6])

    def test_save_heals_impossible_meta_groups(self):
        # low_item ships with the [1..6] default (incl. impossible groups); a plain
        # editor save drops everything outside its family's possible set.
        self.assertEqual(sorted(self.low_item.allowed_meta_groups), [1, 2, 3, 4, 5, 6])
        self.client.force_login(self.manager)
        url = reverse("fitcheck:manage_fit_items", args=[self.fit.pk])
        self.client.post(url, self._formset_data(), follow=True)
        self.low_item.refresh_from_db()
        self.assertEqual(
            set(self.low_item.allowed_meta_groups),
            {EveMetaGroupId.TECH_I, EveMetaGroupId.TECH_II, EveMetaGroupId.FACTION},
        )


class TestAssignmentItemsEditor(PolicyEditorTestCase):
    def test_assignment_editor_offers_only_possible_meta_groups(self):
        from ..services.assignments import attach_fit_to_doctrine

        assignment = attach_fit_to_doctrine(self.fit, self.doctrine, user=self.manager)
        self.client.force_login(self.manager)
        url = reverse("fitcheck:manage_assignment_items", args=[assignment.pk])
        response = self.client.get(url)
        self.assertContains(response, "Faction")
        self.assertNotContains(response, "Officer")


class TestOverrideEndpoints(PolicyEditorTestCase):
    def test_add_include_override(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("fitcheck:override_add", args=[self.low_item.pk]),
            {"type_name": "stasis webifier ii", "mode": "I"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        override = self.low_item.overrides.get()
        self.assertEqual(override.alt_type_id, T.WEB_II)
        self.assertEqual(override.mode, FitItemOverride.Mode.INCLUDE)

    def test_add_override_unknown_name(self):
        self.client.force_login(self.manager)
        self.client.post(
            reverse("fitcheck:override_add", args=[self.low_item.pk]),
            {"type_name": "Not A Module", "mode": "I"},
            follow=True,
        )
        self.assertFalse(self.low_item.overrides.exists())

    def test_cannot_exclude_doctrine_module_itself(self):
        self.client.force_login(self.manager)
        self.client.post(
            reverse("fitcheck:override_add", args=[self.low_item.pk]),
            {"type_name": "Heat Sink II", "mode": "E"},
            follow=True,
        )
        self.assertFalse(self.low_item.overrides.exists())

    def test_remove_override(self):
        self.client.force_login(self.manager)
        self.client.post(
            reverse("fitcheck:override_add", args=[self.low_item.pk]),
            {"type_name": "Imperial Navy Heat Sink", "mode": "E"},
        )
        override = self.low_item.overrides.get()
        old_version = type(self.fit).objects.get(pk=self.fit.pk).version
        self.client.post(
            reverse("fitcheck:override_remove", args=[override.pk]), follow=True
        )
        self.assertFalse(self.low_item.overrides.exists())
        self.fit.refresh_from_db()
        self.assertEqual(self.fit.version, old_version + 1)

    def test_member_cannot_use_override_endpoints(self):
        self.client.force_login(self.member)
        response = self.client.post(
            reverse("fitcheck:override_add", args=[self.low_item.pk]),
            {"type_name": "Stasis Webifier II", "mode": "I"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.low_item.overrides.exists())


class TestModuleSearch(PolicyEditorTestCase):
    """Slot-filtered autocomplete used by the override picker."""

    def test_returns_only_slot_matching_modules(self):
        self.client.force_login(self.manager)
        # Heat Sinks are LOW slot - searching them within LOW section returns hits.
        response = self.client.get(
            reverse("fitcheck:module_search"),
            {"section": "LOW", "q": "Heat Sink"},
        )
        self.assertEqual(response.status_code, 200)
        names = {row["name"] for row in response.json()["results"]}
        self.assertIn("Heat Sink II", names)
        self.assertIn("Imperial Navy Heat Sink", names)
        # Same query against MED section returns none (no Heat Sink fits there).
        response = self.client.get(
            reverse("fitcheck:module_search"),
            {"section": "MED", "q": "Heat Sink"},
        )
        self.assertEqual(response.json()["results"], [])

    def test_empty_in_slot_surfaces_off_slot_hint(self):
        """When the query matches nothing in the requested slot but exists in
        other slots, the picker gets an `off_slot` summary so it can explain
        'exists in LOW, not MED' instead of pretending nothing matched."""
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("fitcheck:module_search"),
            {"section": "MED", "q": "Heat Sink"},
        )
        body = response.json()
        self.assertEqual(body["results"], [])
        # Heat Sinks live in LOW; expect that to show up in off_slot.
        slot_kinds = {entry["slot_kind"] for entry in body["off_slot"]}
        self.assertIn("LOW", slot_kinds)
        # Each entry carries a count > 0.
        for entry in body["off_slot"]:
            self.assertGreater(entry["count"], 0)

    def test_in_slot_hits_do_not_emit_off_slot(self):
        """When the in-slot query already has matches we skip the off-slot
        probe - the second query is wasted work in the happy path."""
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("fitcheck:module_search"),
            {"section": "LOW", "q": "Heat Sink"},
        )
        body = response.json()
        self.assertGreater(len(body["results"]), 0)
        self.assertEqual(body["off_slot"], [])

    def test_booster_section_returns_booster_candidates(self):
        """Regression: Section.BOOSTER was missing from SECTION_TO_SLOT_KINDS, so
        the override search short-circuited to empty for booster rows."""
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("fitcheck:module_search"),
            {"section": "BOOSTER", "q": "Blue Pill"},
        )
        self.assertEqual(response.status_code, 200)
        names = {row["name"] for row in response.json()["results"]}
        self.assertIn("Standard Blue Pill Booster", names)

    def test_requires_min_2_chars(self):
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("fitcheck:module_search"),
            {"section": "LOW", "q": "h"},
        )
        self.assertEqual(response.json()["results"], [])

    def test_unknown_section_returns_empty(self):
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("fitcheck:module_search"),
            {"section": "BOGUS", "q": "Heat"},
        )
        self.assertEqual(response.json()["results"], [])

    def test_member_cannot_call_module_search(self):
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("fitcheck:module_search"),
            {"section": "LOW", "q": "Heat"},
        )
        # Permission denied for non-managers → redirect to login.
        self.assertEqual(response.status_code, 302)


class TestOverrideAddBulk(PolicyEditorTestCase):
    """The bulk endpoint stages many overrides with one fit-version bump."""

    def test_creates_multiple_overrides_with_single_version_bump(self):
        self.client.force_login(self.manager)
        old_version = self.fit.version
        response = self.client.post(
            reverse("fitcheck:override_add_bulk", args=[self.low_item.pk]),
            {"mode": "I", "type_ids": [str(T.HEAT_SINK_IMPERIAL), str(T.HEAT_SINK_AMMATAR)]},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        type_ids = set(self.low_item.overrides.values_list("alt_type_id", flat=True))
        self.assertEqual(type_ids, {T.HEAT_SINK_IMPERIAL, T.HEAT_SINK_AMMATAR})
        self.fit.refresh_from_db()
        self.assertEqual(self.fit.version, old_version + 1)

    def test_skips_off_slot_modules(self):
        """A Stasis Webifier (MED slot) submitted against a LOW-slot item is
        silently skipped rather than creating a nonsense exception."""
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("fitcheck:override_add_bulk", args=[self.low_item.pk]),
            {"mode": "I", "type_ids": [str(T.WEB_II), str(T.HEAT_SINK_IMPERIAL)]},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        type_ids = set(self.low_item.overrides.values_list("alt_type_id", flat=True))
        self.assertEqual(type_ids, {T.HEAT_SINK_IMPERIAL})

    def test_skips_doctrine_module_itself_on_exclude(self):
        """You can't exclude the very type the doctrine row specifies - the
        existing single-add view enforces this; the bulk view does too."""
        self.client.force_login(self.manager)
        self.client.post(
            reverse("fitcheck:override_add_bulk", args=[self.low_item.pk]),
            {"mode": "E", "type_ids": [str(T.HEAT_SINK_II), str(T.HEAT_SINK_IMPERIAL)]},
            follow=True,
        )
        type_ids = set(self.low_item.overrides.values_list("alt_type_id", flat=True))
        # Only the Imperial got through; the doctrine module Heat Sink II was skipped.
        self.assertEqual(type_ids, {T.HEAT_SINK_IMPERIAL})

    def test_empty_submission_is_a_no_op_and_does_not_bump_version(self):
        self.client.force_login(self.manager)
        old_version = self.fit.version
        self.client.post(
            reverse("fitcheck:override_add_bulk", args=[self.low_item.pk]),
            {"mode": "I"},
            follow=True,
        )
        self.fit.refresh_from_db()
        self.assertEqual(self.fit.version, old_version)
        self.assertFalse(self.low_item.overrides.exists())

    def test_invalid_mode_is_rejected(self):
        self.client.force_login(self.manager)
        self.client.post(
            reverse("fitcheck:override_add_bulk", args=[self.low_item.pk]),
            {"mode": "X", "type_ids": [str(T.HEAT_SINK_IMPERIAL)]},
            follow=True,
        )
        self.assertFalse(self.low_item.overrides.exists())

    def test_member_cannot_use_bulk_endpoint(self):
        self.client.force_login(self.member)
        response = self.client.post(
            reverse("fitcheck:override_add_bulk", args=[self.low_item.pk]),
            {"mode": "I", "type_ids": [str(T.HEAT_SINK_IMPERIAL)]},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.low_item.overrides.exists())

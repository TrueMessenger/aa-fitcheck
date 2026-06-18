"""Tests for the Save-to-EVE payload builder + view.

Covers the translation from our `DoctrineFitItem`-shaped fit into ESI's
flag-indexed POST body, the SSO redirect when the user lacks the write
scope, and the happy path (mocked ESI client).
"""

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from ..constants import Section
from ..services.esi_fittings import (
    NoFittingsTokenError,
    build_esi_fitting_payload,
    save_fit_to_eve,
)
from .testdata.factories import add_item, create_fit, create_user
from .testdata.sde_fixtures import T, create_sde_testdata


class TestBuildEsiFittingPayload(TestCase):
    """Section → ESI flag mapping with sequential indexing per slot kind."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_high_slots_get_sequential_hi_flags(self):
        fit = create_fit(None, T.HARBINGER, name="Brawl")
        add_item(fit, Section.HIGH, T.PULSE_LASER_II, quantity=4)

        payload = build_esi_fitting_payload(fit)

        flags = [i["flag"] for i in payload["items"]]
        self.assertEqual(flags, ["HiSlot0", "HiSlot1", "HiSlot2", "HiSlot3"])
        self.assertTrue(all(i["quantity"] == 1 for i in payload["items"]))
        self.assertTrue(all(i["type_id"] == T.PULSE_LASER_II for i in payload["items"]))
        self.assertEqual(payload["ship_type_id"], T.HARBINGER)
        self.assertEqual(payload["name"], "Brawl")

    def test_loaded_charges_emit_extra_item_at_same_slot(self):
        fit = create_fit(None, T.HARBINGER)
        add_item(
            fit, Section.HIGH, T.PULSE_LASER_II, quantity=2,
            charge_type_id=T.MULTIFREQ_L,
        )

        payload = build_esi_fitting_payload(fit)

        # Two slot indexes (HiSlot0, HiSlot1), each with one module + one
        # charge entry sharing the same flag.
        self.assertEqual(payload["items"], [
            {"flag": "HiSlot0", "quantity": 1, "type_id": T.PULSE_LASER_II},
            {"flag": "HiSlot0", "quantity": 1, "type_id": T.MULTIFREQ_L},
            {"flag": "HiSlot1", "quantity": 1, "type_id": T.PULSE_LASER_II},
            {"flag": "HiSlot1", "quantity": 1, "type_id": T.MULTIFREQ_L},
        ])

    def test_slot_indexes_per_section_dont_collide(self):
        fit = create_fit(None, T.HARBINGER)
        add_item(fit, Section.HIGH, T.PULSE_LASER_II, quantity=2)
        add_item(fit, Section.LOW, T.HEAT_SINK_II, quantity=3)

        payload = build_esi_fitting_payload(fit)

        # Each section has its own counter - low slots restart at LoSlot0.
        flags = {(i["flag"], i["type_id"]) for i in payload["items"]}
        self.assertIn(("HiSlot0", T.PULSE_LASER_II), flags)
        self.assertIn(("HiSlot1", T.PULSE_LASER_II), flags)
        self.assertIn(("LoSlot0", T.HEAT_SINK_II), flags)
        self.assertIn(("LoSlot1", T.HEAT_SINK_II), flags)
        self.assertIn(("LoSlot2", T.HEAT_SINK_II), flags)

    def test_bay_sections_collapse_to_single_entry(self):
        fit = create_fit(None, T.HARBINGER)
        add_item(fit, Section.DRONE_BAY, T.HOBGOBLIN_II, quantity=5)
        add_item(fit, Section.CARGO, T.NANITE_PASTE, quantity=50)

        payload = build_esi_fitting_payload(fit)

        # Bays / cargo emit one entry per type with the summed quantity.
        bay_entries = [
            (i["flag"], i["type_id"], i["quantity"]) for i in payload["items"]
        ]
        self.assertIn(("DroneBay", T.HOBGOBLIN_II, 5), bay_entries)
        self.assertIn(("Cargo", T.NANITE_PASTE, 50), bay_entries)

    def test_implants_are_dropped(self):
        """EVE saved fittings don't store implant choices - they live on
        the clone. We silently drop the section instead of failing."""
        fit = create_fit(None, T.HARBINGER)
        add_item(fit, Section.IMPLANT, T.IMPLANT_SM705, quantity=1)

        payload = build_esi_fitting_payload(fit)

        self.assertEqual(payload["items"], [])

    def test_description_falls_back_to_name(self):
        fit = create_fit(None, T.HARBINGER, name="Cold Stare", description="")
        payload = build_esi_fitting_payload(fit)
        self.assertEqual(payload["description"], "Cold Stare")


class TestSaveFitToEve(TestCase):
    """Token absence raises NoFittingsTokenError; the happy path POSTs the
    payload via the mocked ESI client and returns the new fitting_id."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_no_token_raises(self):
        user = create_user("nobody")
        fit = create_fit(None, T.HARBINGER)
        # No esi Token rows for this user - require_scopes().require_valid()
        # returns None and the service raises.
        with self.assertRaises(NoFittingsTokenError):
            save_fit_to_eve(user, character_id=42, fit=fit)

    def test_happy_path_posts_payload(self):
        user = create_user("pilot")
        fit = create_fit(None, T.HARBINGER, name="Brawl")
        add_item(fit, Section.HIGH, T.PULSE_LASER_II, quantity=4)

        fake_token = mock.Mock()
        token_qs = mock.Mock()
        token_qs.require_scopes.return_value.require_valid.return_value.first.return_value = fake_token

        fake_operation = mock.Mock()
        fake_operation.results.return_value = {"fitting_id": 9876}
        fake_client = mock.Mock()
        fake_client.client.Fittings.PostCharactersCharacterIdFittings.return_value = fake_operation

        with mock.patch(
            "fitcheck.services.esi_fittings.esi_client",
            return_value=fake_client,
        ), mock.patch(
            "esi.models.Token.objects.filter",
            return_value=token_qs,
        ):
            fitting_id = save_fit_to_eve(user, 42, fit)

        self.assertEqual(fitting_id, 9876)
        call = fake_client.client.Fittings.PostCharactersCharacterIdFittings.call_args
        self.assertEqual(call.kwargs["character_id"], 42)
        self.assertEqual(call.kwargs["body"]["ship_type_id"], T.HARBINGER)
        self.assertEqual(call.kwargs["body"]["name"], "Brawl")
        self.assertEqual(len(call.kwargs["body"]["items"]), 4)


class TestSaveFitToEveView(TestCase):
    """The view 302s through the SSO grant when the token is missing, and
    delegates to save_fit_to_eve on POST otherwise."""

    @classmethod
    def setUpTestData(cls):
        create_sde_testdata()

    def test_view_redirects_to_sso_when_no_token(self):
        user = create_user("pilot")
        fit = create_fit(None, T.HARBINGER)
        self.client.force_login(user)

        with mock.patch(
            "fitcheck.services.esi_fittings.save_fit_to_eve",
            side_effect=NoFittingsTokenError(),
        ):
            response = self.client.post(
                reverse("fitcheck:save_fit_to_eve", args=[fit.pk])
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("fittings-write-token", response.url)
        self.assertIn(f"next_fit={fit.pk}", response.url)

    def test_view_flashes_success_on_happy_path(self):
        user = create_user("pilot")
        fit = create_fit(None, T.HARBINGER)
        self.client.force_login(user)

        with mock.patch(
            "fitcheck.services.esi_fittings.save_fit_to_eve",
            return_value=4242,
        ):
            response = self.client.post(
                reverse("fitcheck:save_fit_to_eve", args=[fit.pk]), follow=True
            )

        self.assertEqual(response.status_code, 200)
        messages = [str(m) for m in response.context["messages"]]
        self.assertTrue(any("4242" in m for m in messages), messages)

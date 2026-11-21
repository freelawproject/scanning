"""The volunteers: a group confined to the card review (``scanning.volunteers``)."""

from django.contrib.auth.models import AnonymousUser, Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse

from scanning import opinion_cards, volunteers
from scanning.models import OpinionCard
from scanning.tests.test_opinion_cards import CardTestCase, document


class VolunteerTestCase(CardTestCase):
    def setUp(self):
        super().setUp()
        self.group, _ = Group.objects.get_or_create(name=volunteers.GROUP)
        self.volunteer = self.make_user(username="vol")
        self.volunteer.groups.add(self.group)
        opinion_cards.sync(self.opinion, document(self.held()))
        self.card = self.row()

    def assign(self, user=None):
        self.card.assigned_to = user or self.volunteer
        self.card.save(update_fields=["assigned_to"])

    def card_kwargs(self):
        return {"pk": self.opinion.scan_id, "opinion_pk": self.opinion.pk}


class TestTheRule(VolunteerTestCase):
    def test_a_member_who_is_not_staff_is_a_volunteer(self):
        self.assertTrue(volunteers.is_volunteer(self.volunteer))

    def test_staff_outsiders_and_strangers_are_not(self):
        staff = self.make_staff_user()
        staff.groups.add(self.group)
        self.assertFalse(volunteers.is_volunteer(staff))
        self.assertFalse(volunteers.is_volunteer(self.make_user()))
        self.assertFalse(volunteers.is_volunteer(AnonymousUser()))
        self.assertFalse(volunteers.is_volunteer(None))

    def test_the_home_is_their_cards(self):
        self.assertEqual(
            volunteers.home_url(), reverse("opinion_card_review") + "?mine=1"
        )


class TestTheGate(VolunteerTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.volunteer)

    def test_every_other_page_goes_to_their_cards(self):
        for name in ("scan_list", "opinion_list", "stats", "queue"):
            answer = self.client.get(reverse(name))
            self.assertRedirects(
                answer, volunteers.home_url(), fetch_redirect_response=False
            )
        answer = self.client.get(
            reverse("opinion_review", kwargs={"pk": self.opinion.pk})
        )
        self.assertRedirects(
            answer, volunteers.home_url(), fetch_redirect_response=False
        )

    def test_a_request_that_wants_no_page_is_refused_as_json(self):
        answer = self.client.get(
            reverse("scan_list"), HTTP_ACCEPT="application/json"
        )
        self.assertEqual(answer.status_code, 403)
        self.assertEqual(answer.json()["message"], volunteers.REFUSED_MESSAGE)
        answer = self.client.post(
            reverse(
                "send_back_opinion_card",
                kwargs={"pk": self.opinion.scan_id, "card_pk": self.card.pk},
            ),
            {},
            content_type="application/json",
        )
        self.assertEqual(answer.status_code, 403)
        self.assertEqual(answer.json()["status"], "error")

    def test_the_allowed_pages_open(self):
        for name in ("opinion_card_review", "volunteer_faq", "profile"):
            self.assertEqual(
                self.client.get(reverse(name)).status_code, 200, name
            )

    def test_a_regular_user_is_not_gated(self):
        self.client.force_login(self.make_user())
        self.assertEqual(
            self.client.get(reverse("scan_list")).status_code, 200
        )

    def test_a_staff_member_of_the_group_keeps_the_site(self):
        staff = self.make_staff_user()
        staff.groups.add(self.group)
        self.client.force_login(staff)
        self.assertEqual(
            self.client.get(reverse("scan_list")).status_code, 200
        )


class TestTheirCards(VolunteerTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.volunteer)

    def test_the_page_shows_their_cards_alone(self):
        address = reverse("opinion_card_review")
        answer = self.client.get(address)
        self.assertTrue(answer.context["filters"]["mine"])
        self.assertEqual(answer.context["card_total"], 0)
        self.assertNotContains(answer, f'id="opinion-{self.opinion.pk}"')

        self.assign()
        answer = self.client.get(address + "?reason=characters")
        self.assertEqual(answer.context["card_total"], 1)
        self.assertContains(answer, f'id="opinion-{self.opinion.pk}"')
        self.assertNotContains(answer, "Open the review page")

    def test_a_curator_sees_the_review_link(self):
        self.client.force_login(self.make_user())
        answer = self.client.get(reverse("opinion_card_review"))
        self.assertContains(answer, "Open the review page")

    def test_the_rows_are_theirs_alone(self):
        address = reverse("opinion_card_rows", kwargs=self.card_kwargs())
        self.assertEqual(self.client.get(address).json()["cards"], [])
        self.assign()
        self.assertEqual(len(self.client.get(address).json()["cards"]), 1)

    def test_an_answer_on_another_card_is_refused(self):
        address = reverse(
            "answer_opinion_card",
            kwargs={"pk": self.opinion.scan_id, "card_pk": self.card.pk},
        )
        answer = self.client.post(
            address, {"answer": "held"}, content_type="application/json"
        )
        self.assertEqual(answer.status_code, 403)
        self.assertEqual(answer.json()["message"], volunteers.CARD_MESSAGE)
        self.card.refresh_from_db()
        self.assertIsNone(self.card.answer)

        self.assign()
        answer = self.client.post(
            address, {"answer": "held"}, content_type="application/json"
        )
        self.assertEqual(answer.status_code, 200, answer.content)
        self.card.refresh_from_db()
        self.assertEqual(self.card.answer, "held")

    def test_the_pdf_link_needs_a_card_of_theirs(self):
        address = reverse("opinion_pdf_url", kwargs=self.card_kwargs())
        answer = self.client.get(address)
        self.assertEqual(answer.status_code, 403)
        self.assertEqual(answer.json()["message"], volunteers.CARD_MESSAGE)
        self.assign()
        # Past the gate: no PDF is written for this opinion, a 404.
        self.assertEqual(self.client.get(address).status_code, 404)

    def test_the_header_offers_their_cards_and_the_help(self):
        answer = self.client.get(reverse("opinion_card_review"))
        self.assertContains(answer, "My cards")
        self.assertContains(answer, reverse("volunteer_faq"))
        self.assertNotContains(answer, f'href="{reverse("stats")}"')
        self.assertNotContains(answer, f'href="{reverse("opinion_list")}"')

        self.client.force_login(self.make_user())
        answer = self.client.get(reverse("opinion_card_review"))
        self.assertNotContains(answer, "My cards")
        self.assertContains(answer, f'href="{reverse("stats")}"')


class TestTheCommand(VolunteerTestCase):
    def test_the_group_gets_a_chunk_each(self):
        other = self.make_user(username="zed")
        other.groups.add(self.group)

        call_command(
            "assign_cards",
            group=volunteers.GROUP,
            reason="characters",
            count=1,
        )

        # One open card, so the first member by username gets it.
        self.card.refresh_from_db()
        self.assertEqual(self.card.assigned_to, self.volunteer)
        self.assertFalse(
            OpinionCard.objects.filter(assigned_to=other).exists()
        )

        call_command("assign_cards", group=volunteers.GROUP, release=True)
        self.card.refresh_from_db()
        self.assertIsNone(self.card.assigned_to)

    def test_a_dry_run_assigns_nothing(self):
        call_command(
            "assign_cards",
            group=volunteers.GROUP,
            reason="characters",
            count=1,
            dry_run=True,
        )
        self.card.refresh_from_db()
        self.assertIsNone(self.card.assigned_to)

    def test_the_refusals(self):
        with self.assertRaises(CommandError):
            call_command("assign_cards", group="nobody", reason="characters")
        with self.assertRaises(CommandError):
            call_command("assign_cards", reason="characters")
        with self.assertRaises(CommandError):
            call_command("assign_cards", "vol")

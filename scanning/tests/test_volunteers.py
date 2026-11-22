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

    def test_a_recheck_never_touches_the_session(self):
        """The answer lives in the cache, by user: a session write would
        save the session and move its expiry out, and a recheck every
        ten minutes would keep an active account signed in for ever."""
        from django.contrib.sessions.models import Session
        from django.core.cache import cache

        self.client.force_login(self.volunteer)
        key = self.client.session.session_key
        before = Session.objects.get(session_key=key).expire_date
        cache.delete(volunteers.cache_key(self.volunteer))

        answer = self.client.get(reverse("opinion_card_review"))

        self.assertEqual(answer.status_code, 200)
        self.assertTrue(answer.context["filters"]["mine"])
        self.assertEqual(
            Session.objects.get(session_key=key).expire_date, before
        )
        self.assertIs(cache.get(volunteers.cache_key(self.volunteer)), True)

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
        for name in (
            "opinion_card_review",
            "volunteer_faq",
            "volunteer_practice",
            "profile",
        ):
            self.assertEqual(
                self.client.get(reverse(name)).status_code, 200, name
            )

    def test_another_apps_view_keeps_its_own_gate(self):
        """The admin sends a non-staff user to its login, never to the
        cards; the dev toolbar's polls would loop on a redirect."""
        answer = self.client.get(reverse("admin:index"))
        self.assertEqual(answer.status_code, 302)
        self.assertIn("/admin/login/", answer["Location"])

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
        # No review page for the card's link either: the script reads
        # the address off the section and offers none when it is blank.
        self.assertContains(answer, 'data-review-url=""')

    def test_a_curator_sees_the_review_link(self):
        self.client.force_login(self.make_user())
        answer = self.client.get(reverse("opinion_card_review"))
        self.assertContains(answer, "Open the review page")
        self.assertContains(
            answer,
            'data-review-url="'
            + reverse("opinion_review", kwargs={"pk": self.opinion.pk}),
        )

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

    def test_the_page_offers_no_scan_volume_or_mine_filter(self):
        """A volunteer's cards are their own, so the scan, volume and
        "Assigned to me" filters are left off; a curator keeps them."""
        answer = self.client.get(reverse("opinion_card_review"))
        self.assertNotContains(answer, 'name="scan"')
        self.assertNotContains(answer, 'name="volume"')
        self.assertNotContains(answer, 'name="mine"')
        self.assertContains(answer, 'name="level"')

        self.client.force_login(self.make_user())
        answer = self.client.get(reverse("opinion_card_review"))
        self.assertContains(answer, 'name="scan"')
        self.assertContains(answer, 'name="volume"')
        self.assertContains(answer, 'name="mine"')

    def test_the_header_offers_their_cards_and_the_help(self):
        answer = self.client.get(reverse("opinion_card_review"))
        self.assertContains(answer, "My cards")
        self.assertContains(answer, reverse("volunteer_faq"))
        self.assertContains(answer, f'href="{reverse("volunteer_practice")}"')
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

    def test_staff_and_disabled_members_get_no_card(self):
        """The members are the ones the gate counts: a card given to a
        staff member or a disabled account is locked away."""
        staff = self.make_staff_user(username="aaa-staff")
        staff.groups.add(self.group)
        gone = self.make_user(username="aab-gone", is_active=False)
        gone.groups.add(self.group)

        call_command(
            "assign_cards",
            group=volunteers.GROUP,
            reason="characters",
            count=1,
        )

        self.card.refresh_from_db()
        self.assertEqual(self.card.assigned_to, self.volunteer)

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
        with self.assertRaises(CommandError):
            call_command(
                "assign_cards", "vol", group=volunteers.GROUP, reason="name"
            )


class TestThePractice(VolunteerTestCase):
    def test_every_example_is_whole(self):
        """Each example has its image under the static files, readings,
        and an answer that names one of them, a blank, or typed words."""
        from pathlib import Path

        from django.contrib.staticfiles import finders

        from scanning import volunteer_practice

        slugs = [e["slug"] for e in volunteer_practice.EXAMPLES]
        self.assertEqual(len(slugs), len(set(slugs)))
        for example in volunteer_practice.EXAMPLES:
            with self.subTest(example["slug"]):
                found = finders.find(
                    "scanning/faq/practice/" + example["image"]
                )
                self.assertTrue(
                    found and Path(found).is_file(), example["image"]
                )
                self.assertTrue(example["readings"])
                answer = example["answer"]
                if isinstance(answer, dict):
                    self.assertTrue(answer.get("typed"))
                else:
                    self.assertIn(
                        answer, range(0, len(example["readings"]) + 1)
                    )
                self.assertTrue(example["why"])

    def test_the_page_draws_every_example(self):
        from scanning import volunteer_practice

        self.client.force_login(self.volunteer)
        answer = self.client.get(reverse("volunteer_practice"))
        self.assertEqual(answer.status_code, 200)
        for example in volunteer_practice.EXAMPLES:
            self.assertContains(answer, f'data-slug="{example["slug"]}"')
        self.assertContains(answer, "scanning/practice.js")
        # The FAQ offers it, and the gate lets a volunteer in.
        faq = self.client.get(reverse("volunteer_faq"))
        self.assertContains(faq, reverse("volunteer_practice"))


class TestTheClaim(VolunteerTestCase):
    """A volunteer with an empty list claims a batch of short three-way
    word cards, of opinions with no picture, and sees their total."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.volunteer)

    def claim(self):
        return self.client.post(reverse("claim_opinion_cards"), follow=True)

    def test_an_empty_list_offers_the_claim_and_the_claim_assigns(self):
        page = self.client.get(reverse("opinion_card_review"))
        self.assertContains(page, reverse("claim_opinion_cards"))
        self.assertContains(page, f"Claim {opinion_cards.CLAIM_COUNT} cards")

        answer = self.claim()

        self.card.refresh_from_db()
        self.assertEqual(self.card.assigned_to, self.volunteer)
        self.assertContains(answer, "You claimed 1 card(s)")
        self.assertNotContains(answer, reverse("claim_opinion_cards"))

    def test_a_volunteer_who_holds_a_card_is_refused(self):
        self.assign()
        before = OpinionCard.objects.filter(assigned_to=self.volunteer).count()

        answer = self.claim()

        self.assertContains(answer, "Finish the cards you have first")
        self.assertEqual(
            OpinionCard.objects.filter(assigned_to=self.volunteer).count(), before
        )

    def test_an_opinion_with_a_picture_is_not_claimed(self):
        self.opinion.figure_digest = "abc"
        self.opinion.save(update_fields=["figure_digest"])

        answer = self.claim()

        self.card.refresh_from_db()
        self.assertIsNone(self.card.assigned_to)
        self.assertContains(answer, "no cards to claim")

    def test_a_card_assigned_to_someone_else_is_not_claimed(self):
        self.assign(self.make_user(username="other"))
        self.claim()
        self.card.refresh_from_db()
        self.assertNotEqual(self.card.assigned_to, self.volunteer)

    def test_only_short_three_way_word_cards_are_claimable(self):
        self.assertTrue(opinion_cards.claimable(self.card))
        for change in (
            lambda c: c["readings"].pop(),
            lambda c: c["readings"][0].update(word="a run of more than three words"),
            lambda c: c.update(gap=True),
            lambda c: c.update(crop=None),
        ):
            row = OpinionCard.objects.get(pk=self.card.pk)
            change(row.card)
            self.assertFalse(opinion_cards.claimable(row))

    def test_the_page_shows_their_answered_total(self):
        self.card.answered_by = self.volunteer
        self.card.save(update_fields=["answered_by"])
        page = self.client.get(reverse("opinion_card_review"))
        self.assertContains(page, "You've answered <b>1</b> card.")

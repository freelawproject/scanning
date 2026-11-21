"""Hand out open card rows to a volunteer, by reason.

The cards page (``/opinions/cards/?mine=1``) shows a person the cards
assigned to them. This command assigns the next open, unassigned cards
of a reason (and of named volumes, if any), whole opinions at a time
so a volunteer never shares an opinion's crops with another, oldest
volume first.

Examples:

    # Say which cards would go, and change nothing.
    docker exec scanning-daemon python manage.py \\
        assign_cards anabelle --reason name --count 200 --dry-run

    # Give 200 Name cards to a volunteer.
    docker exec scanning-daemon python manage.py \\
        assign_cards anabelle --reason name --count 200

    # Take back every open card assigned to a volunteer.
    docker exec scanning-daemon python manage.py assign_cards anabelle --release

    # Give every member of the volunteers group 200 Name cards each.
    docker exec scanning-daemon python manage.py \\
        assign_cards --group volunteers --reason name --count 200
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from scanning import blocking_types, opinion_cards
from scanning.models import OpinionCard, OpinionReviewStatus


class Command(BaseCommand):
    help = "Assign open card rows of a reason to a volunteer."

    def add_arguments(self, parser):
        parser.add_argument("username", nargs="?")
        parser.add_argument(
            "--group",
            help="Every member of this auth group, each given --count "
            "cards (or each released), in username order.",
        )
        parser.add_argument(
            "--reason", choices=[slug for slug, _ in blocking_types.TYPES]
        )
        parser.add_argument("--count", type=int, default=200)
        parser.add_argument(
            "--level",
            choices=("blocking", "minor"),
            default="blocking",
            help="Blocking cards (the default), or the minor warnings.",
        )
        parser.add_argument("--scan", nargs="+", type=int, default=[])
        parser.add_argument(
            "--release",
            action="store_true",
            help="Take back the volunteer's open cards instead.",
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        if not options["reason"] and not options["release"]:
            raise CommandError("Name a --reason, or --release.")
        for user in self._users(options):
            if options["release"]:
                self._release(user, options)
            else:
                self._assign(user, options)

    def _users(self, options) -> list:
        """The volunteers named: one user, or every member of a group."""
        if options["group"]:
            group = Group.objects.filter(name=options["group"]).first()
            if group is None:
                raise CommandError(f"No group {options['group']!r}.")
            users = list(group.user_set.order_by("username"))
            if not users:
                raise CommandError(f"The group {group.name!r} has no member.")
            return users
        if not options["username"]:
            raise CommandError("Name a user, or a --group.")
        user = (
            get_user_model()
            .objects.filter(username=options["username"])
            .first()
        )
        if user is None:
            raise CommandError(f"No user {options['username']!r}.")
        return [user]

    def _release(self, user, options) -> None:
        rows = OpinionCard.objects.filter(
            assigned_to=user, status__in=opinion_cards.WAITING
        )
        if not options["dry_run"]:
            count = rows.update(assigned_to=None)
        else:
            count = rows.count()
        self.stdout.write(f"released {count} card(s) of {user}")

    def _assign(self, user, options) -> None:
        rows = OpinionCard.objects.filter(
            reason=options["reason"],
            status__in=opinion_cards.WAITING,
            assigned_to__isnull=True,
            opinion__status=OpinionReviewStatus.READY_FOR_TEXT_REVIEW,
        )
        if options["level"] == "minor":
            rows = rows.filter(check_name=opinion_cards.MINOR_CHECK)
        else:
            rows = rows.filter(check_name__in=opinion_cards.CARD_CHECKS)
        if options["scan"]:
            rows = rows.filter(opinion__scan_id__in=options["scan"])
        # Whole opinions, in the order of the volumes, until the count.
        picked, total = [], 0
        for opinion_id, count in self._per_opinion(rows):
            if total and total + count > options["count"]:
                break
            picked.append(opinion_id)
            total += count
        chosen = rows.filter(opinion_id__in=picked)
        if not options["dry_run"]:
            chosen.update(assigned_to=user)
        verb = "would assign" if options["dry_run"] else "assigned"
        self.stdout.write(
            f"{verb} {total} {options['reason']} card(s) in "
            f"{len(picked)} opinion(s) to {user}"
        )

    def _per_opinion(self, rows):
        from django.db.models import Count

        return (
            rows.values_list("opinion_id")
            .annotate(n=Count("id"))
            .order_by("opinion__scan_id", "opinion_id")
        )

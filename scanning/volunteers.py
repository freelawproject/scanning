"""The volunteers: a group whose members answer cards and see nothing else.

A volunteer is a logged-in member of the ``volunteers`` group who is
not staff (:func:`is_volunteer`, **the one rule**). The gate
(``middleware.VolunteerGateMiddleware``) confines such a user to
:data:`URL_NAMES`: the cards page and the three endpoints its script
calls, the FAQ, and the account pages. Every other address answers a
redirect to :func:`home_url`, their cards, or a 403 as JSON when the
request wants no page. The cards page forces its ``mine`` filter for a
volunteer (``views_cards._filters``), and the rows, the answer and the
redacted-PDF link refuse a card or an opinion not assigned to them.

A staff member of the group is staff: the rule reads ``is_staff``
first, so a curator put in the group to try the page keeps the site.

**The answer is kept in the cache** for :data:`RECHECK_SECONDS`, by
user, and primed at login, so a page costs no query for it: the gate
and the header ask on every request, and the view tests pin their
query counts. It is never written on the session: a session write
saves the session and moves its expiry out by ``SESSION_COOKIE_AGE``,
and a recheck every ten minutes would keep an active account signed in
for ever. A change of the group reaches a signed-in person within
that time, or at once when they sign in again.

Cards are handed out with ``assign_cards``, by reason, whole opinions
at a time; ``--group volunteers`` gives every member a chunk.
"""

from __future__ import annotations

from django.contrib.auth.signals import user_logged_in
from django.core.cache import cache
from django.db.models import Q
from django.dispatch import receiver
from django.urls import reverse

#: The name of the Django auth group.
GROUP = "volunteers"

#: The url names a volunteer may reach: the cards page, the rows feed,
#: the answer and the redacted-PDF link its script calls, the claim of a
#: batch, the FAQ, and
#: the account pages. A name not here is sent to :func:`home_url`.
URL_NAMES = frozenset(
    {
        "opinion_card_review",
        "opinion_card_rows",
        "answer_opinion_card",
        "claim_opinion_cards",
        "opinion_pdf_url",
        "volunteer_faq",
        "volunteer_practice",
        "volunteer_practice_done",
        "login",
        "logout",
        "profile",
        "password_change",
    }
)

#: The url name of a volunteer's home, their cards.
HOME = "opinion_card_review"

#: How long the cached answer is trusted before the group is read again.
RECHECK_SECONDS = 600

#: The group of the people who finished the practice cards once. A
#: group and not a field, so staff see it and clear it in the admin.
PRACTICED_GROUP = "volunteers-practiced"

REFUSED_MESSAGE = "This page is not part of the card review."
CARD_MESSAGE = "This card is not assigned to you."


def cache_key(user) -> str:
    """Return the cache key of a user's answer."""
    return f"volunteer:{user.pk}"


def is_volunteer(user) -> bool:
    """Return whether a user is a volunteer: logged in, in the group,
    and not staff. The group is read once per :data:`RECHECK_SECONDS`
    per user, through the cache, and never off the session.

    :param user: ``request.user``, or None.
    :rtype: bool
    """
    if user is None or not user.is_authenticated or user.is_staff:
        return False
    key = cache_key(user)
    flag = cache.get(key)
    if flag is None:
        flag = user.groups.filter(name=GROUP).exists()
        cache.set(key, flag, RECHECK_SECONDS)
    return bool(flag)


def of_request(request) -> bool:
    """Return :func:`is_volunteer` for a request, read once per request."""
    cached = getattr(request, "_is_volunteer", None)
    if cached is None:
        cached = is_volunteer(getattr(request, "user", None))
        request._is_volunteer = cached
    return cached


def has_practiced(user) -> bool:
    """Return whether a person may claim cards without the practice.

    The first claim waits on the practice cards, once: a person who
    finished them (:data:`PRACTICED_GROUP`), or who has answered or
    holds a card already (they were taught by hand before the practice
    was required), has practiced.

    :param user: The person.
    :rtype: bool
    """
    from scanning.models import OpinionCard

    if user.groups.filter(name=PRACTICED_GROUP).exists():
        return True
    return OpinionCard.objects.filter(
        Q(answered_by=user) | Q(assigned_to=user)
    ).exists()


def mark_practiced(user) -> None:
    """Record that a person finished the practice cards."""
    from django.contrib.auth.models import Group

    group, _ = Group.objects.get_or_create(name=PRACTICED_GROUP)
    user.groups.add(group)


def home_url() -> str:
    """Return the address of a volunteer's own cards."""
    return reverse(HOME) + "?mine=1"


@receiver(user_logged_in)
def _prime_at_login(sender, request, user, **kwargs) -> None:
    """Read the group at login, so the first page costs no query."""
    is_volunteer(user)

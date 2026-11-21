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

**The answer is kept on the session**, stamped at login and read
again every :data:`RECHECK_SECONDS`, so a page costs no query for it:
the gate and the header ask on every request, and the view tests pin
their query counts. A change of the group reaches a logged-in person
within that time, or at once when they sign in again.

Cards are handed out with ``assign_cards``, by reason, whole opinions
at a time; ``--group volunteers`` gives every member a chunk.
"""

from __future__ import annotations

import time

from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver
from django.urls import reverse

#: The name of the Django auth group.
GROUP = "volunteers"

#: The url names a volunteer may reach: the cards page, the rows feed,
#: the answer and the redacted-PDF link its script calls, the FAQ, and
#: the account pages. A name not here is sent to :func:`home_url`.
URL_NAMES = frozenset(
    {
        "opinion_card_review",
        "opinion_card_rows",
        "answer_opinion_card",
        "opinion_pdf_url",
        "volunteer_faq",
        "login",
        "logout",
        "profile",
        "password_change",
    }
)

#: The url name of a volunteer's home, their cards.
HOME = "opinion_card_review"

#: The session key of the stamp, ``{"flag", "user", "at"}``.
SESSION_KEY = "volunteer"

#: How long a stamp on the session is trusted before the group is read
#: again.
RECHECK_SECONDS = 600

REFUSED_MESSAGE = "This page is not part of the card review."
CARD_MESSAGE = "This card is not assigned to you."


def is_volunteer(user, session=None) -> bool:
    """Return whether a user is a volunteer: logged in, in the group,
    and not staff.

    :param user: ``request.user``, or None.
    :param session: ``request.session``, which holds the answer for
        :data:`RECHECK_SECONDS`; without one the group is read.
    :rtype: bool
    """
    if user is None or not user.is_authenticated or user.is_staff:
        return False
    now = time.time()
    if session is not None:
        stamp = session.get(SESSION_KEY)
        if (
            isinstance(stamp, dict)
            and stamp.get("user") == user.pk
            and now - float(stamp.get("at") or 0) < RECHECK_SECONDS
        ):
            return bool(stamp.get("flag"))
    flag = user.groups.filter(name=GROUP).exists()
    if session is not None:
        session[SESSION_KEY] = {"flag": flag, "user": user.pk, "at": now}
    return flag


def of_request(request) -> bool:
    """Return :func:`is_volunteer` for a request, read once per request."""
    cached = getattr(request, "_is_volunteer", None)
    if cached is None:
        cached = is_volunteer(
            getattr(request, "user", None), getattr(request, "session", None)
        )
        request._is_volunteer = cached
    return cached


def home_url() -> str:
    """Return the address of a volunteer's own cards."""
    return reverse(HOME) + "?mine=1"


@receiver(user_logged_in)
def _stamp_at_login(sender, request, user, **kwargs) -> None:
    """Stamp the session at login, so the first page costs no query."""
    session = getattr(request, "session", None)
    if session is not None:
        is_volunteer(user, session)

from django.conf import settings


def inject_settings(request):
    """Inject specific settings into every template context.

    :param request: The current HTTP request.
    :type request: django.http.HttpRequest
    :returns: A dictionary with DEBUG and DEVELOPMENT flags.
    :rtype: dict[str, bool]
    """
    return {
        "DEBUG": settings.DEBUG,
        "DEVELOPMENT": settings.DEVELOPMENT,
    }


def waiting_repairs(request):
    """Inject the count of repair requests that wait (issue #249).

    The header shows it beside the "Repairs" link, so a scanner sees
    work from any page. One indexed count, for a logged-in user only.

    :param request: The current HTTP request.
    :type request: django.http.HttpRequest
    :returns: A dictionary with ``waiting_repairs_count``.
    :rtype: dict[str, int]
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"waiting_repairs_count": 0}
    from scanning import repairs

    return {"waiting_repairs_count": repairs.waiting_count()}


def volunteer(request):
    """Inject whether the viewer is a volunteer (``volunteers.is_volunteer``).

    The header shows a volunteer their cards and the FAQ alone, and the
    cards page hides the review link from them.

    :param request: The current HTTP request.
    :type request: django.http.HttpRequest
    :returns: A dictionary with ``is_volunteer``.
    :rtype: dict[str, bool]
    """
    from scanning import volunteers

    return {"is_volunteer": volunteers.of_request(request)}

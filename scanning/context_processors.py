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

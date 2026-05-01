from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def url_replace(context, **kwargs):
    """Return the current query string with the given params replaced or removed.

    Pass a key with an empty string to remove it from the query string.

    :param context: The template context (must contain ``request``).
    :returns: A query string like ``?foo=bar&baz=1``, or ``""`` if empty.
    :rtype: str
    """
    query = context["request"].GET.copy()
    for key, value in kwargs.items():
        if value:
            query[key] = value
        else:
            query.pop(key, None)
    return "?" + query.urlencode() if query else ""

import factory
from django.contrib.auth.models import User

from scanning.models import OpinionScan, OpinionStatus, Reporter, Scan, Source, Status


class UserFactory(factory.django.DjangoModelFactory):
    """Factory for creating User instances.

    Default declarations:

    - ``username``: sequential (``user0``, ``user1``, ...).
    - ``email``: derived from username.
    - ``password``: ``testpass123`` (hashed via ``set_password``).
    """

    class Meta:
        model = User
        skip_postgeneration_save = True

    username = factory.Sequence(lambda n: f"user{n}")
    email = factory.LazyAttribute(lambda o: f"{o.username}@example.com")

    @factory.post_generation
    def password(obj, create, extracted, **kwargs):
        """Set and persist the user's password.

        :param obj: The User instance.
        :type obj: User
        :param create: Whether the instance was created (vs built).
        :type create: bool
        :param extracted: An explicit password, or None for default.
        :type extracted: str | None
        """
        password = extracted or "testpass123"
        obj.set_password(password)
        if create:
            obj.save(update_fields=["password"])


class ReporterFactory(factory.django.DjangoModelFactory):
    """Factory for creating Reporter instances.

    Default declarations:

    - ``short_name``: ``a``.
    - ``full_name``: ``Atlantic Reporter``.
    """

    class Meta:
        model = Reporter
        django_get_or_create = ("short_name",)

    short_name = "a"
    full_name = "Atlantic Reporter"


class ScanFactory(factory.django.DjangoModelFactory):
    """Factory for creating Scan instances.

    Default declarations:

    - ``reporter``: auto-created via ``ReporterFactory``.
    - ``volume``: sequential starting at 1.
    - ``number_of_pages``: 100.
    - ``start_page``: 1.
    - ``end_page``: 100.
    - ``source``: ``Source.FULL``.
    - ``original_pdf``: dummy PDF file.
    - ``uploaded_by``: auto-created via ``UserFactory``.
    - ``status``: ``Status.UPLOADED``.
    """

    class Meta:
        model = Scan

    reporter = factory.SubFactory(ReporterFactory)
    volume = factory.Sequence(lambda n: n + 1)
    number_of_pages = 100
    start_page = 1
    end_page = 100
    source = Source.FULL
    original_pdf = factory.django.FileField(
        filename="test.pdf", data=b"%PDF-1.4 test"
    )
    uploaded_by = factory.SubFactory(UserFactory)
    status = Status.UPLOADED


class OpinionScanFactory(factory.django.DjangoModelFactory):
    """Factory for creating OpinionScan instances.

    Default declarations:

    - ``scan``: None (standalone opinion).
    - ``reporter``: auto-created via ``ReporterFactory``.
    - ``volume``: sequential starting at 1.
    - ``original_pdf``: dummy PDF file.
    - ``uploaded_by``: auto-created via ``UserFactory``.
    - ``status``: ``OpinionStatus.NO_STATUS``.
    - ``page_start``: 1.
    - ``page_end``: 10.
    """

    class Meta:
        model = OpinionScan

    scan = None
    reporter = factory.SubFactory(ReporterFactory)
    volume = factory.Sequence(lambda n: n + 1)
    original_pdf = factory.django.FileField(
        filename="opinion.pdf", data=b"%PDF-1.4 opinion"
    )
    uploaded_by = factory.SubFactory(UserFactory)
    status = OpinionStatus.NO_STATUS
    page_start = 1
    page_end = 10

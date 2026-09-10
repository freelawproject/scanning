import factory
from django.contrib.auth.models import User

from scanning.models import (
    ExternalJob,
    JobEngine,
    JobProvider,
    JobStage,
    JobStatus,
    OpinionBoundary,
    OpinionScan,
    OpinionStatus,
    PageEdit,
    Reporter,
    Scan,
    Source,
    Status,
    Volume,
)


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
    def password(
        obj: User, create: bool, extracted: str | None, **kwargs
    ) -> None:
        """Set and persist the user's password.

        :param obj: The User instance.
        :param create: Whether the instance was created (vs built).
        :param extracted: An explicit password, or None for default.
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


class VolumeFactory(factory.django.DjangoModelFactory):
    """Factory for creating Volume instances.

    Default declarations:

    - ``reporter``: auto-created via ``ReporterFactory``.
    - ``volume_number``: sequential starting at 1.
    - ``expected_start_page``: 1.
    - ``expected_end_page``: 100.
    """

    class Meta:
        model = Volume

    reporter = factory.SubFactory(ReporterFactory)
    volume_number = factory.Sequence(lambda n: n + 1)
    expected_start_page = 1
    expected_end_page = 100


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


class ExternalJobFactory(factory.django.DjangoModelFactory):
    """Factory for creating ExternalJob instances.

    Defaults to a volume-level job, since that shape needs no opinion.
    For an opinion-level stage pass both ``stage`` and ``opinion``, which
    the ``job_opinion_matches_stage`` constraint requires together.

    Default declarations:

    - ``scan``: auto-created via ``ScanFactory``.
    - ``opinion``: None (volume-level work).
    - ``stage``: ``JobStage.DETECT``.
    - ``engine``: ``JobEngine.BLACKLETTER``.
    - ``provider``: ``JobProvider.RUNPOD``.
    - ``status``: ``JobStatus.PENDING``.
    - ``run``: 1.
    - ``shard_index``: 0, ``shard_count``: 1.
    """

    class Meta:
        model = ExternalJob

    scan = factory.SubFactory(ScanFactory)
    opinion = None
    stage = JobStage.DETECT
    engine = JobEngine.BLACKLETTER
    provider = JobProvider.RUNPOD
    status = JobStatus.PENDING
    run = 1
    shard_index = 0
    shard_count = 1


class PageEditFactory(factory.django.DjangoModelFactory):
    """Factory for creating PageEdit instances (issue #214).

    Defaults to a page number a curator typed on page 1, since that
    shape needs no image and no anchor. For an insert pass
    ``kind=PageEdit.Kind.INSERT_PAGE``, ``pdf_page=None`` and an
    ``anchor_pdf_page``, which the ``page_edit_address_matches_kind``
    constraint requires together.

    Default declarations:

    - ``scan``: auto-created via ``ScanFactory``.
    - ``kind``: ``PageEdit.Kind.SET_NUMBER``.
    - ``author``: auto-created via ``UserFactory``.
    - ``pdf_page``: 1.
    - ``value``: ``"1"``.
    """

    class Meta:
        model = PageEdit

    scan = factory.SubFactory(ScanFactory)
    kind = PageEdit.Kind.SET_NUMBER
    author = factory.SubFactory(UserFactory)
    pdf_page = 1
    value = "1"


class OpinionBoundaryFactory(factory.django.DjangoModelFactory):
    """Factory for creating OpinionBoundary instances (issue #240, PR C).

    Defaults to one computed opinion over the first two pages of the
    original, addressed in the original's space. For a curator's row
    pass ``origin=OpinionBoundary.Origin.HUMAN`` and a ``kind``.

    Default declarations:

    - ``scan``: auto-created via ``ScanFactory``.
    - ``origin``: ``OpinionBoundary.Origin.COMPUTED``.
    - ``start_source_page``: 1; ``start_page_index``: 0.
    - ``start_x``, ``start_y``: 72.0, 100.0.
    - ``end_source_page``: 2; ``end_page_index``: 1.
    - ``end_x``, ``end_y``: 540.0, 700.0.
    - ``ordinal``: sequential from 0.
    """

    class Meta:
        model = OpinionBoundary
        skip_postgeneration_save = True

    scan = factory.SubFactory(ScanFactory)
    origin = OpinionBoundary.Origin.COMPUTED
    start_source_page = 1
    start_page_index = 0
    start_x = 72.0
    start_y = 100.0
    end_source_page = 2
    end_page_index = 1
    end_x = 540.0
    end_y = 700.0
    ordinal = factory.Sequence(lambda n: n)


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

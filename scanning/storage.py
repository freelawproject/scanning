from django.core.files.storage import FileSystemStorage
from storages.backends.s3boto3 import S3Boto3Storage, S3ManifestStaticStorage


class SubDirectoryS3ManifestStaticStorage(S3ManifestStaticStorage):
    location = "static"
    default_acl = "public-read"
    # Fall back to unhashed URLs instead of raising ValueError
    # when a file is missing from the staticfiles manifest.
    manifest_strict = False


class PrivateS3Storage(S3Boto3Storage):
    """S3 storage for private file uploads (scanned documents).

    Used for archiving originals and storing approved deliverables.
    """

    default_acl = "private"
    file_overwrite = False
    querystring_auth = True
    querystring_expire = 300  # 5-minute signed URLs
    custom_domain = (
        False  # disable custom domain so signed URLs use the S3 hostname
    )

    def __init__(self, **kwargs):
        from django.conf import settings

        kwargs.setdefault(
            "bucket_name", settings.AWS_PRIVATE_STORAGE_BUCKET_NAME
        )
        super().__init__(**kwargs)


class LocalProcessingStorage(FileSystemStorage):
    """Always-local storage for files used during processing.

    Ensures .path works regardless of the default storage backend,
    so the daemon pipeline can use fitz.open(), Path(), etc.

    Reads ``MEDIA_ROOT`` lazily so that ``@override_settings`` in
    tests is respected.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @property
    def base_location(self):
        """Return MEDIA_ROOT at access time, not import time.

        :return: The current MEDIA_ROOT setting.
        :rtype: str
        """
        from django.conf import settings

        return settings.MEDIA_ROOT

    @property
    def location(self):
        """Return the absolute path to MEDIA_ROOT.

        :return: The resolved MEDIA_ROOT path.
        :rtype: str
        """
        import os

        return os.path.abspath(self.base_location)

    @property
    def base_url(self):
        """Return MEDIA_URL at access time, not import time.

        :return: The current MEDIA_URL setting.
        :rtype: str
        """
        from django.conf import settings

        return settings.MEDIA_URL

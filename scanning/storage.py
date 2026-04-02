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
    """

    def __init__(self, **kwargs):
        from django.conf import settings

        kwargs.setdefault("location", settings.MEDIA_ROOT)
        kwargs.setdefault("base_url", settings.MEDIA_URL)
        super().__init__(**kwargs)

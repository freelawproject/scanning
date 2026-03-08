from storages.backends.s3boto3 import S3Boto3Storage, S3ManifestStaticStorage


class SubDirectoryS3ManifestStaticStorage(S3ManifestStaticStorage):
    location = "static"
    # Fall back to unhashed URLs instead of raising ValueError
    # when a file is missing from the staticfiles manifest.
    manifest_strict = False


class PrivateS3Storage(S3Boto3Storage):
    """S3 storage for private file uploads (scanned documents)."""

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

import environ

from ..django import DEVELOPMENT

env = environ.FileAwareEnv()

# S3
if DEVELOPMENT:
    AWS_ACCESS_KEY_ID = env("AWS_DEV_ACCESS_KEY_ID", default="")
    AWS_SECRET_ACCESS_KEY = env("AWS_DEV_SECRET_ACCESS_KEY", default="")
else:
    AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID", default="")
    AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY", default="")

AWS_STORAGE_BUCKET_NAME = env(
    "AWS_STORAGE_BUCKET_NAME",
    default="com-freelawproject-scanning-storage",
)
AWS_PRIVATE_STORAGE_BUCKET_NAME = env(
    "AWS_PRIVATE_STORAGE_BUCKET_NAME",
    default="com-freelawproject-scanning-private-storage",
)

# Region the buckets live in. Until presigned PUTs arrived nothing
# needed this: SigV2 URLs carry no region, and S3 redirects a
# misdirected GET. SigV4 encodes the region into the credential scope,
# so a mismatch is a hard ``AuthorizationQueryParametersError`` -- and
# without this set, boto3 falls back to ``us-east-1``.
#
# Read by django-storages under this exact name, which is why it's
# named for the library rather than for us, and passed explicitly by
# the two clients we build ourselves (``s3_sync._s3_client`` and
# ``runpod_client._s3``). Both buckets must be in it; check with
# ``aws s3api get-bucket-location --bucket <name>``.
AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", default="us-west-2")

AWS_S3_CUSTOM_DOMAIN = env(
    "AWS_S3_CUSTOM_DOMAIN",
    default=f"{AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com",
)
AWS_DEFAULT_ACL = None
AWS_QUERYSTRING_AUTH = True
AWS_S3_MAX_MEMORY_SIZE = 16 * 1024 * 1024

if DEVELOPMENT:
    AWS_STORAGE_BUCKET_NAME = "dev-com-freelawproject-scanning-storage"
    AWS_PRIVATE_STORAGE_BUCKET_NAME = (
        "dev-com-freelawproject-scanning-private-storage"
    )
    AWS_S3_CUSTOM_DOMAIN = f"{AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com"

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

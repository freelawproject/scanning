"""Doctor client settings (bitonal conversion, issue #176).

Doctor runs the 1-bit conversion pass: one HTTP request per shard,
input from a presigned GET, output to a presigned PUT (see
``scanning/doctor_client.py``). It mints no job id and has no status
endpoint, so the response is the completion signal and an S3
``head_object`` on the result key is the recovery path.

The conversion parameters live here rather than in doctor, so retuning
them is a scanning deploy instead of a doctor release.
"""

import environ

env = environ.FileAwareEnv()

# Master switch for dispatching shards to doctor. There is no
# in-process fallback (issue #173), so with this off an environment
# uploads, shards and browses but produces no ``bitonal.pdf``, and the
# viewer falls back to the original.
#
# On by default, so a deploy converts without an env change. Safe as a
# default because the pipeline also requires S3 to be active before
# creating any job (``services._can_convert``): doctor reads its input
# through a presigned GET, so an environment whose shards never reached
# the bucket parks its volumes unconverted rather than queueing work
# that cannot run. A dev environment *with* credentials does dispatch
# for real, and the in-cluster host below will not resolve from a
# laptop -- set this False there unless you have a reachable doctor.
DOCTOR_ENABLED = env.bool("DOCTOR_ENABLED", default=True)

# Base URL, no trailing slash. Defaults to the shared CourtListener
# instance, so enabling the stage needs no secret-store change.
# Fully qualified deliberately: an unqualified ``cl-doctor`` does not
# resolve from the ``scanning`` namespace, so CourtListener's own
# default cannot be copied (issue #158).
DOCTOR_HOST = env.str(
    "DOCTOR_HOST",
    default="http://cl-doctor.court-listener.svc.cluster.local:5050",
)

# Rasterization resolution and grayscale threshold. These are the
# values the retired in-process pass used (``blackletter.api.bitonal``
# defaults, never overridden), so a converted volume matches every
# ``bitonal.pdf`` already in the corpus. Deliberately not doctor's own
# 300/128.
DOCTOR_BITONAL_DPI = env.int("DOCTOR_BITONAL_DPI", default=200)
DOCTOR_BITONAL_THRESHOLD = env.int("DOCTOR_BITONAL_THRESHOLD", default=160)

# How many conversions may run at once, which is also how many rows one
# submit tick claims. Doctor serves sync views on asgiref's
# single-thread executor, so a pod converts one shard at a time and the
# real ceiling is the replica count (~52 in production). 8 is ~15% of
# that, leaving CourtListener's own traffic room; sending more than
# doctor can run just holds sockets open.
DOCTOR_MAX_CONCURRENCY = env.int("DOCTOR_MAX_CONCURRENCY", default=8)

# Connect and read timeouts (seconds). A preempted pod should fail
# fast; the read timeout sits well above a shard's expected 25-45s.
# Abandoning the read does not abandon the work -- doctor runs to
# completion and its PUT lands -- which is why the confirm pass checks
# S3 rather than resubmitting.
DOCTOR_CONNECT_TIMEOUT = env.int("DOCTOR_CONNECT_TIMEOUT", default=5)
DOCTOR_READ_TIMEOUT = env.int("DOCTOR_READ_TIMEOUT", default=300)

# Wall-clock ceiling (seconds) for one submitted attempt. Generous
# against a 25-45s shard because it bounds how long we wait for a lost
# *answer*, not how long a conversion may take: past this with nothing
# at the result key, the attempt is written off and retried.
DOCTOR_JOB_DEADLINE_SECONDS = env.int(
    "DOCTOR_JOB_DEADLINE_SECONDS", default=900
)

# Attempts per shard before its job is failed. Doctor runs on spot
# nodes with a 30s termination grace and redeploys on every merged PR,
# so losing an in-flight conversion is routine.
DOCTOR_MAX_ATTEMPTS = env.int("DOCTOR_MAX_ATTEMPTS", default=3)

# Lifetime (seconds) of the presigned URLs handed to doctor. Mirrors
# RUNPOD_PRESIGNED_TTL: generous, so a signature never dies before the
# work lands, with the job deadline far inside it. Each URL is scoped to
# one method on one key. AWS SigV4 max is 7 days.
DOCTOR_PRESIGNED_TTL = env.int("DOCTOR_PRESIGNED_TTL", default=86400)
